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

from __future__ import annotations  # allows use of "forward annotations" of undefined classes within type hints
import argparse
from decimal import Decimal
import enum
import functools
import json
import logging
import os
import sys
import time
from typing import Optional
import warnings
import xml.etree.ElementTree as ET

import requests
import requests_oauthlib


_OAUTH_GET_TOKEN_URL = 'https://api.login.yahoo.com/oauth2/get_token'
_OAUTH_REFRESH_TOKEN_URL = 'https://api.login.yahoo.com/oauth2/get_token'
_OAUTH_REQUEST_AUTH_URL = 'https://api.login.yahoo.com/oauth2/request_auth'
_TMP_DIR = '/tmp/'
_XMLNS = 'http://fantasysports.yahooapis.com/fantasy/v2/base.rng'
_NS = {'xmlns': _XMLNS}

logger = logging.getLogger(__name__)


def main(argv=None):
  """Main function, only run if executed as script. Not run if module is imported."""
  if argv is None:
    argv = sys.argv[1:]
  setup_log_handlers()
  parser = make_parser()
  args = parser.parse_args(argv)
  creds = obtain_credentials(args, YahooFantasyOAuth.required_creds)
  auth = YahooFantasyOAuth(creds['client_id'], creds['client_secret'],
                           force_refresh_token=args.force_refresh_token)
  fantasyapi = YahooFantasyAPI(auth)
  myleague = YahooLeagueResource(fantasyapi, game_key='nfl', league_id=args.league_id)
  myteams = myleague.teams
  print(myteams[0].name)
  print(myteams[0].get_week_score(3))
  print(myteams[0].get_week_score(2))
  print(myteams[0].managers)
  # myleague = fantasyapi.get_league(game_key='nfl', league_id=args.league_id)
  # print(type(myleague.raw))
  # myleague.get_teams()
  print(fantasyapi.query_count)


def setup_log_handlers():
  global logger
  logger.setLevel(logging.DEBUG)
  formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
  fh = logging.FileHandler('yahoo_log.txt', encoding='utf-8')
  fh.setLevel(logging.DEBUG)
  fh.setFormatter(formatter)
  logger.addHandler(fh)


def make_parser() -> argparse.ArgumentParser:
  """Create and return argument parser."""
  parser = argparse.ArgumentParser()
  parser.add_argument('--league_id', type=str)
  parser.add_argument('--client_id', type=str)
  parser.add_argument('--client_secret', type=str)
  parser.add_argument('--redirect_uri', type=str, default='oob')
  parser.add_argument('--force_refresh_token', action='store_true')
  return parser


class YahooFantasyAPI:
  _BASE_URL = 'https://fantasysports.yahooapis.com/fantasy/v2'

  def __init__(self, oauth_client) -> None:
    self.oauth_client = oauth_client
    self.query_count = 0  # keep track of number of non-cached queries

  def get_resource(self, resource_name, resource_key) -> requests.Response:
    """request a Resource as defined at https://developer.yahoo.com/fantasysports/guide/#resources-and-collections"""
    uri = f'{resource_name}/{resource_key}'
    return self.get_uri(uri)

  @functools.cache
  def _get_uri_cached(self, uri) -> requests.Response:
    return self._get_uri_live(uri)

  def _get_uri_live(self, uri) -> requests.Response:
    url = f'{self._BASE_URL}/{uri}'
    self.query_count += 1
    return self.oauth_client.get(url)

  def get_uri(self, uri, bypasscache=False) -> requests.Response:
    if bypasscache:
      return self._get_uri_live(uri)
    else:
      return self._get_uri_cached(uri)

  def __repr__(self) -> str:
    return f'YahooFantasyAPI({repr(self.oauth_client)})'


class YahooManager:
  def __init__(self,
               team: YahooTeamResource,
               manager_id: str,
               name: str,
               guid: str) -> None:
    self.manager_id = manager_id
    self.name = name
    self.guid = guid
    self.team = team

  def __eq__(self, other):
    return isinstance(other, YahooManager) and (self.guid == other.guid)

  @property
  def nickname(self):
    return self.name

  @nickname.setter
  def nickname(self, value):
    self.name = value

  def __repr__(self) -> str:
    return f'YahooManager({repr(self.team)}, {repr(self.manager_id)}, {repr(self.name)}, {repr(self.guid)})'


class YahooResourceXML:
  def __init__(self, resource_name, resource_key) -> None:
    self.resource_name = resource_name
    self.resource_key = resource_key

  @property
  def raw(self):
    return self.api.get_resource(self.resource_name, self.resource_key).text

  def __eq__(self, other) -> bool:
    return isinstance(other, YahooResourceXML) and (other.resource_key == self.resource_key)

  def fetch_tag(self, s):
    root = ET.fromstring(self.raw)
    node = root.find(f'./xmlns:{self.resource_name}', _NS)
    tag_vals = xml_elementtree_todict(node)
    return tag_vals[s]


class YahooTeamResource(YahooResourceXML):
  def __init__(self,
               api: YahooFantasyAPI,
               parent_league: YahooLeagueResource,
               team_key: str,
               **kwargs) -> None:
    self.api = api
    self.league = parent_league
    self.team_key = team_key
    self._props = kwargs
    self._managers = None
    self._scores = None
    super().__init__('team', self.team_key)

  def __repr__(self) -> str:
    return f'YahooTeamResource({repr(self.api)}, {repr(self.league)}, {repr(self.team_key)}, {repr(self.name)})'

  @property
  def name(self):
    _name = self._props.get('name', None)
    if _name is None:
      _name = self.fetch_tag('name')
      self._props['name'] = _name
    return _name

  @property
  def managers(self):
    if self._managers is None:
      uri = f'team/{self.team_key}'
      s = self.api.get_uri(uri).text
      root = ET.fromstring(s)
      _managers = []
      for manager_node_i in root.findall('./xmlns:team/xmlns:managers/xmlns:manager', _NS):
        manager_vals_i = xml_elementtree_todict(manager_node_i)
        manager_guid_i = manager_vals_i['guid']
        manager_id_i = manager_vals_i['manager_id']
        manager_name_i = manager_vals_i['nickname']
        manager_i = YahooManager(self, manager_id_i, manager_name_i, manager_guid_i)
        _managers.append(manager_i)
      self._managers = _managers
    return self._managers

  def __eq__(self, other) -> bool:
    return isinstance(other, YahooTeamResource) and (other.team_key == self.team_key)

  @property
  def scores(self):
    uri = f'team/{self.team_key}/matchups'
    s = self.api.get_uri(uri, bypasscache=True).text  # bypass cache to make sure we get latest live score
    root = ET.fromstring(s)
    _scores = []
    for matchup_node_i in root.findall('./xmlns:team/xmlns:matchups/xmlns:matchup', _NS):
      # each matchup contains two teams
      # As a side effect to looking up this team, we're going to get a bunch of other teams' scores
      # I could do something useful with them... but I ignore them here
      team_points_i = None
      team_projected_points_i = None
      matchup_vals_i = xml_elementtree_todict(matchup_node_i)
      week_i = matchup_vals_i['week']
      status_i = MatchupStatus.from_yahoostatus(matchup_vals_i['status'])
      team_nodes = matchup_node_i.findall('./xmlns:teams/xmlns:team', _NS)
      for team_node_j in team_nodes:
        team_key_j = team_node_j.find('xmlns:team_key', _NS).text
        if team_key_j != self.team_key:
          continue
        team_points_j = Decimal(team_node_j.find('./xmlns:team_points/xmlns:total', _NS).text)
        team_projected_points_j = Decimal(team_node_j.find('./xmlns:team_projected_points/xmlns:total', _NS).text)
        team_points_i = team_points_j
        team_projected_points_i = team_projected_points_j
        break
      # finally:
      matchup_score_i = TeamMatchupScore(self, int(week_i), status_i, team_points_i, team_projected_points_i)
      _scores.append(matchup_score_i)
    return _scores

  def get_week_score(self, week: int) -> list[TeamMatchupScore]:
    """Return this teams' scores in given weeks"""
    week_idx = week - 1
    return self.scores[week_idx]

class YahooLeagueResource(YahooResourceXML):
  def __init__(self,
               api: YahooFantasyAPI,
               league_id: int,
               game_key: str,
               league_key: Optional[str] = None):
    self.api = api
    self.league_id = league_id
    self.game_key = game_key
    if league_key is None:
      league_key = f'{game_key}.l.{league_id}'
    self.league_key = league_key
    super().__init__('league', self.league_key)
    self._teams = None

  @property
  def raw(self):
    return self.api.get_resource('league', self.league_key).text

  def __repr__(self) -> str:
    s = f'YahooLeagueResource({repr(self.api)}, {repr(self.league_id)}, {repr(self.game_key)}, {repr(self.league_key)})'
    return s

  @property
  def teams(self) -> list[YahooTeamResource]:
    if self._teams is None:
      _teams = []
      uri = f'league/{self.league_key}/teams'
      s = self.api.get_uri(uri).text
      root = ET.fromstring(s)
      for team_node_i in root.findall('./xmlns:league/xmlns:teams/xmlns:team', _NS):
        team_vals_i = xml_elementtree_todict(team_node_i)
        team_key_i = team_vals_i['team_key']
        team_name_i = team_vals_i['name']
        team_i = YahooTeamResource(self.api, self, team_key_i, name=team_name_i)
        _teams.append(team_i)
      self._teams = _teams
    return self._teams


class YahooGameResource(YahooResourceXML):
  def __init__(self, game_key):
    self.game_key = game_key
    super().__init__('game', self.game_key)


class YahooFantasyOAuth:
  required_creds = {'client_id', 'client_secret', 'redirect_uri'}

  def __init__(self, client_id, client_secret, redirect_uri=None, force_refresh_token=False):
    self.client_id = client_id
    self.client_secret = client_secret
    if redirect_uri is None:
      redirect_uri = 'oob'
    self.redirect_uri = redirect_uri
    self.token = None
    self.client = None
    if force_refresh_token:
      self.update_client(expires_at=-10)
    else:
      self.update_client()

  def __repr__(self) -> str:
    return f'YahooFantasyOAuth({repr(self.client_id)}, {repr(self.client_secret)}, {repr(self.redirect_uri)})'

  def get(self, url: str) -> requests.Response:
    resp = self.client.get(url)
    if resp.status_code != 200:
      raise RuntimeError(f'get{url} returned status {resp.status_code} instead of 200.'
                         ' Full response text:\n' + resp.text)
    return resp

  def update_client(self, expires_at=None) -> None:
    if self.token is None:
      self.load_token()
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

  def token_updater(self, token) -> None:
    logger.debug('token updater was called')
    self.token = token
    self.save_token()

  def save_token(self) -> None:
    token_path = get_token_filepath(self.client_id)
    with open(token_path, 'w', encoding="utf-8") as f:
      json.dump(self.token, f)
    logger.debug(f'token saved to {token_path}')

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
      logger.debug('loaded token file at {token_path}')
    if not success:
      logger.debug('Fetching a new token.')
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
    logger.debug(f'token expires_in updated from {last_expires_in} to {next_expires_in}')
    logger.debug(f'token expires_at updated from {last_expires_at} to {next_expires_at}')
    self.save_token()

#  TODO: refactor YahooFantasyOauth to use a token class, like I started below
# class YahooOauthToken:
#   def __init__(self,
#                client_id: str,
#                value: Optional[dict] = None) -> None:
#     self.client_id = client_id
#     self.value = value

#   @property
#   def filepath(self):
#     return

#   def save(self):
#     raise NotImplementedError

#   def load(self):
#     if self.value is not None:
#       raise ValueError('Loading new token from disk will overwrite existing value!')
#     raise NotImplementedError

#   def update_expiration(self):
#     raise NotImplementedError


class YahooMatchup:
  def __init__(self,
               week: str | int,
               team1: YahooTeamResource,
               team2: YahooTeamResource) -> None:
    self.week = week
    teams_sorted = sorted([team1, team2])
    self.team_a = teams_sorted[0]
    self.team_b = teams_sorted[1]
    self.teams = set(teams_sorted)

  def __eq__(self, other) -> bool:
    return (set(self.teams) == set(other.teams)) and (self.week == other.week)


class MatchupStatus(enum.Enum):
  PREEVENT = 1
  MIDEVENT = 2
  POSTEVENT = 3

  @classmethod
  def from_yahoostatus(cls, s: str) -> MatchupStatus:
    return cls[s.upper()]


class TeamMatchupScore:
  def __init__(self,
               team: YahooTeamResource,
               week: str | int,
               status: MatchupStatus,
               points: Decimal,
               projected_points: Decimal) -> None:
    self.team = team
    self.week = week
    self.status = status
    self.points = points
    self.projected_points = projected_points

  @property
  def complete(self) -> bool:
    return self.status == MatchupStatus.POSTEVENT

  @property
  def inprogress(self) -> bool:
    return self.status == MatchupStatus.INPROGRESS

  @property
  def future(self) -> bool:
    return self.status == MatchupStatus.PREEVENT

  def __str__(self) -> str:
    return f'Team {self.team.name} week {self.week} ({self.status}) score: {self.points} actual vs {self.projected_points} projected'

  def __repr__(self) -> str:
    return self.__str__()


def get_token_filepath(client_id: str | int) -> str:
  return os.path.join(_TMP_DIR, f'oauth2_token_{client_id}.json')


def obtain_credentials(args, required_creds):
  """Obtain the client_id, client_secret, and redirect_uri credentials manually if not passed as args."""
  creds = {}
  args_dict = vars(args)
  for cred_label in required_creds:
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


def xml_elementtree_todict(node, strip_ns=_XMLNS):
  """Convert xml to dict where each tag is a key and text is value and namespace is stripped from keys."""
  d = {}
  for child in node:
    if strip_ns:
      k = child.tag.removeprefix(f'{{{strip_ns}}}')
    else:
      k = child.tag
    d[k] = child.text.strip()
  return d


if __name__ == '__main__':
  main()


# todos
# don't cache gets if they relate to any mid-event scoring
# add sub-resources of team like matchups, stats (by season or by date.. does the api allow filtering stats by week?)
# todo: add sub-resources of league: /settings, /standings, /scoreboard
# write lt/gt/etc functions to allow teams to be sorted (by team key)
# maybe split yahoo api and yahoo resources into separate files
# maybe write some methods for the api that automatically convert the whole xml stuff to a json. Then the yahoo api file could become a standalone command line executable of some sort that returns json