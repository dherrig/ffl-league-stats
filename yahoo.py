#!/usr/bin/env python3

# yahoo uses oauth1 or 2? I assume oauth2. One doc mentions, "3-legged OAuth"
# yahoo uses the "Authorization Code Grant" flow (aka web application flow)
# https://oauth.net/code/python/
# https://developer.yahoo.com/oauth/
# https://developer.yahoo.com/oauth2/guide/
# https://developer.yahoo.com/fantasysports/guide/
# https://github.com/requests/requests-oauthlib
# https://developer.yahoo.com/oauth2/guide/flows_authcode/
# NOTE some of the auth and token urls in yahoo tutorials seem wrong or out of
# date. This last oauth2 guide from yahoo has the correct newest urls

import argparse
import json
import os
import sys
import time
import warnings

import requests_oauthlib


_OAUTH_GET_TOKEN_URL = 'https://api.login.yahoo.com/oauth2/get_token'
_OAUTH_REFRESH_TOKEN_URL = 'https://api.login.yahoo.com/oauth2/get_token'
_OAUTH_REQUEST_AUTH_URL = 'https://api.login.yahoo.com/oauth2/request_auth'
_TMP_DIR = '/tmp/'
# _LEAGUE_KEYS = {'2023': 423, '2022': 414, '2021': 406, '2020': 399}


def make_parser():
  parser = argparse.ArgumentParser()
  parser.add_argument('--league_id', type=str)
  parser.add_argument('--client_id', type=str)
  parser.add_argument('--client_secret', type=str)
  parser.add_argument('--redirect_uri', type=str, default='oob')
  parser.add_argument('--force_refresh_token', action='store_true')
  return parser


def main(argv=None):
  if argv is None:
    argv = sys.argv[1:]
  parser = make_parser()
  args = parser.parse_args(argv)
  creds = obtain_credentials(args)
  x = YahooOAuth(creds['client_id'], creds['client_secret'], args.league_id,
                 force_refresh_token=args.force_refresh_token)
  x.test_auth()


class YahooOAuth:
  def __init__(self, client_id, client_secret, league_id, redirect_uri=None, force_refresh_token=False):
    self.client_id = client_id
    self.client_secret = client_secret
    self.league_id = league_id
    self.league_url = f'https://fantasysports.yahooapis.com/fantasy/v2/league/nfl.l.{self.league_id}'
    if redirect_uri is None:
      redirect_uri = 'oob'
    self.redirect_uri = redirect_uri
    self.token = None
    self.client = None
    if force_refresh_token:
      self.update_client(expires_at=-10)
    else:
      self.update_client()

  def get(self, url):
    return self.client.get(url)

  def update_client(self, expires_at=None):
    if self.token is None:
      self.load_token()
    print(self.token)
    self.update_token_expiration(force_value=expires_at)
    extra = {
        'client_id': self.client_id,
        'client_secret': self.client_secret,
    }
    self.client = requests_oauthlib.OAuth2Session(self.client_id,
                                                  redirect_uri=self.redirect_uri,
                                                  token=self.token,
                                                  auto_refresh_url=_OAUTH_REFRESH_TOKEN_URL,
                                                  auto_refresh_kwargs=extra,
                                                  token_updater=self.token_updater
                                                  )
    self.test_auth()

  def token_updater(self, token):
    print('token updater was called')
    self.token = token
    self.save_token()

  def save_token(self):
    token_path = get_token_filepath(self.client_id)
    with open(token_path, 'w', encoding="utf-8") as f:
      json.dump(self.token, f)
    print(f'[token saved to {token_path}]')

  def load_token(self):
    token_path = get_token_filepath(self.client_id)
    success = False
    try:
      with open(token_path, 'r', encoding="utf-8") as f:
        loaded_token = json.load(f)
        self.token = loaded_token
        success = True
    except FileNotFoundError:
      print(f'no token file saved at {token_path}.')
    except json.decoder.JSONDecodeError:
      print(f'file at {token_path} is not valid json.')
      os.remove(token_path)
    else:
      print(f'loaded token file at {token_path}')
    if not success:
      print('Fetching a new token.')
      self.get_new_token()

  def get_new_token(self):
    """Go through the user auth flow and get a new token"""
    self.client = requests_oauthlib.OAuth2Session(self.client_id,
                                                  redirect_uri=self.redirect_uri)
    auth_url, _ = self.client.authorization_url(_OAUTH_REQUEST_AUTH_URL)
    print(f'Please go to {auth_url} and authorize access.')
    auth_code = input('Enter the secret code from the auth url: ')
    self.token = self.client.fetch_token(_OAUTH_GET_TOKEN_URL,
                                         client_secret=self.client_secret,
                                         code=auth_code)
    self.test_auth()
    self.save_token()

  def update_token_expiration(self, force_value=None):
    last_expires_in = self.token['expires_in']
    last_expires_at = self.token['expires_at']
    if force_value is not None:
      next_expires_in = force_value
      next_expires_at = time.time() + force_value
    else:
      next_expires_in = self.token['expires_at'] - time.time()
      next_expires_at = last_expires_at
    if (force_value is None) and (next_expires_in > last_expires_in):
      warnings.warn(f'possibly bad expires_in value. token.expires_in increased from {last_expires_in}'
                    f' to {next_expires_in}')
    self.token['expires_in'] = next_expires_in
    self.token['expires_at'] = next_expires_at
    print(f'[token expires_in updated from {last_expires_in} to {next_expires_in}]')
    print(f'[token expires_at updated from {last_expires_at} to {next_expires_at}]')
    self.save_token()

  def test_auth(self):
    """Verify auth by fetching a protected url"""
    resp = self.get(self.league_url)
    if resp.status_code != 200:
      raise RuntimeError(f'Fetching {self.league_url} returned status {resp.status_code}'
                         f' instead of 200. Text:' + '\n' + resp.text)
    # todo: handle errors. maybe try refreshing token


def league_api_url_currentyear(league_id):
  return f'https://fantasysports.yahooapis.com/fantasy/v2/league/nfl.l.{league_id}'


def get_token_filepath(client_id):
  return os.path.join(_TMP_DIR, f'oauth2_token_{client_id}.json')


def obtain_credentials(args):
  """Obtain the client_id, client_secret, and redirect_uri credentials manually if not passed as args."""
  creds = {}
  args_dict = vars(args)
  for cred_label in ('client_id', 'client_secret', 'redirect_uri'):
    if cred_label in args_dict:
      this_cred = args_dict[cred_label]
    else:
      this_cred = manual_cred_input(cred_label)
    creds[cred_label] = this_cred
  return creds


def manual_cred_input(label):
  cred_valid = False
  while not cred_valid:
    new_cred = input(f'Enter {label}: ')
    print(f'You entered {label} = "{new_cred}"')
    conf = input('Enter "y" to confirm or any other key to redo: ')
    cred_valid = conf.lower() == 'y'
  return new_cred


if __name__ == '__main__':
  main()
