#!/usr/bin/env python3

import argparse
import functools
import itertools
import math
import multiprocessing
import sys
from collections import defaultdict
import tqdm
from tqdm.contrib.concurrent import process_map

import pandas as pd
import yahoo


MAX_PROCESSES = multiprocessing.cpu_count()


def main(argv=None):
  if argv is None:
    argv = sys.argv[1:]
  parser = get_parser()
  parser = yahoo_parser(parser)
  args = parser.parse_args(argv)

  print('Connecting to yahoo api')
  creds = yahoo.obtain_credentials(args, yahoo.YahooFantasyOAuth.required_creds)
  auth = yahoo.YahooFantasyOAuth(creds['client_id'], creds['client_secret'],
                                 force_refresh_token=args.force_refresh_token)
  fantasyapi = yahoo.YahooFantasyAPI(auth)
  myleague = yahoo.YahooLeagueResource(fantasyapi, game_key='nfl', league_id=args.league_id)
  myteams = myleague.teams
  print(f'We made {fantasyapi.query_count} queries so far')

  print('Creating Dataframe of all scores')
  teams_df, scores_df, num_weeks = dfs_from_yahoo(myteams, maxweek=args.weeks)
  print(f'We made {fantasyapi.query_count} queries so far')

  simmer = ScheduleSimmer(teams_df, scores_df, num_weeks)

  print('Simulating schedules')
  team_keys = list(teams_df.index)
  all_records = simmer.simulate_all_schedules(team_keys)
  print('ALL RECORDS:')
  for team_key_i in all_records.keys():
    print(teams_df.loc[team_key_i])
    print_records_dict(all_records[team_key_i], num_weeks)


def dfs_from_yahoo(yh_teams: list[yahoo.YahooTeamResource], maxweek=None):
  """Return dataframes containing team details and scores"""
  # teams_df columns: team key, team name, manager name
  # scores_df columns: team key, week, points
  team_keys = [x.team_key for x in yh_teams]
  team_names = [x.name for x in yh_teams]
  # mgr_names = [[y.name for y in x.managers] for x in yh_teams]
  mgr_names = ['NA' for _ in yh_teams]  # remove manager names for now to reduce number of queries. they are all returning 'hidden' for some reason
  teams_index = pd.Index(team_keys, name='team_key')
  teams_df = pd.DataFrame(zip(team_names, mgr_names), index=teams_index, columns=['team_name', 'manager_name'])
  scores_lst = []

  for i, team_i in enumerate(yh_teams):
    team_i_scores = [y.points for y in team_i.scores]
    if (i == 0) and (maxweek is None):
      maxweek = len(team_i_scores)
    team_i_scores = team_i_scores[:maxweek]
    scores_lst.extend(team_i_scores)

  week_nums = range(1, 1 + maxweek)
  scores_index = pd.MultiIndex.from_product([team_keys, week_nums], names=['team_key', 'week_num'])
  scores_df = pd.DataFrame(scores_lst, index=scores_index, columns=['points'])
  return teams_df, scores_df, maxweek


class ScheduleSimmer:
  def __init__(self,
               teams_df: pd.DataFrame,
               scores_df: pd.DataFrame,
               num_weeks: int) -> None:
    self.teams_df = teams_df
    self.scores_df = scores_df
    self.nweeks = num_weeks
    self.nteams = len(self.teams_df)

  @functools.cache
  def _compute_h2h_result_cache(self,
                                team_a_key: str,
                                team_b_key: str,
                                week_n: int) -> tuple[bool, bool, bool]:
    """
    Return the head to head result as if team_a played team_b in week_n.

    For more compact caching, teamid_a and teamid_b should be passed in sorted order, teamid_a < teamid_b.

    Returns:
      (bool, bool, bool): (team a wins, team b wins, tie). Only one item should be True in each return result.
    """
    team_a_score = get_week_score(self.scores_df, team_a_key, week_n)
    team_b_score = get_week_score(self.scores_df, team_b_key, week_n)
    team_a_win = team_a_score > team_b_score
    teamab_draw = team_a_score == team_b_score
    team_b_win = not (teamab_draw or team_a_win)
    return team_a_win, team_b_win, teamab_draw

  def compute_h2h_result(self,
                         team_a_key: str,
                         team_b_key: str,
                         week_n: int) -> tuple[bool, bool, bool]:
    if team_a_key > team_b_key:
      x = self._compute_h2h_result_cache(team_b_key, team_a_key, week_n)
      return x[1], x[0], x[2]
    else:
      return self._compute_h2h_result_cache(team_a_key, team_b_key, week_n)

  def simulate_all_schedules(self, team_keys: list[str]) -> dict:
    all_records = {x: defaultdict(int) for x in team_keys}
    total_schedules = math.factorial(self.nteams - 1)
    for team_i_key in team_keys:
      all_schedules_iter = permute_schedules(team_i_key, team_keys)
      print(f'Simulating {total_schedules} schedules for team {team_i_key}')
      with tqdm.tqdm(total=total_schedules) as pbar:
        for j, sched_j in enumerate(all_schedules_iter):
          # print(f'{j} of {total_schedules}')
          # print(team_i_key, sched_j)
          record_j = self.compute_record(team_i_key, sched_j)
          # print(record_j)
          all_records[team_i_key][record_j] += 1
          if j % 100 == 0:
            pbar.update(100)
      print(team_i_key)
      print_records_dict(all_records[team_i_key], self.nweeks)
    return all_records

  def simulate_all_schedules_poolseasons(self, team_keys: list[str]) -> dict:
    """Simulate all schedules, but run multiple seasons in parallel"""
    all_records = {x: defaultdict(int) for x in team_keys}
    total_schedules = math.factorial(self.nteams - 1)
    tqdm.tqdm.set_lock(multiprocessing.RLock())
    for team_i_key in team_keys:
      all_schedules_iter = permute_schedules(team_i_key, team_keys)
      print(f'Simulating {total_schedules} schedules for team {team_i_key} using process_map')

      # _get_record = lambda x: self.compute_record(team_i_key, x)
      p = multiprocessing.Pool()
      with multiprocessing.Pool(MAX_PROCESSES) as p:
        records_lst = list(tqdm.tqdm(p.imap(functools.partial(self.compute_record, team_i_key), all_schedules_iter), total=total_schedules))
      # records_lst = process_map(functools.partial(self.compute_record, team_i_key), all_schedules_iter, max_workers=MAX_PROCESSES, total=total_schedules)
      all_records[team_i_key] = {k: records_lst.count(k) for k in set(records_lst)}
      print(team_i_key)
      print_records_dict(all_records[team_i_key], self.nweeks)
    return all_records

  def compute_record(self,
                     team_key: str,
                     schedule):
    num_otherteams = self.nteams - 1
    n_win = 0
    n_loss = 0
    n_draw = 0
    for week_idx in range(self.nweeks):
      week_num = week_idx + 1
      week_idx_wrap = week_idx % num_otherteams
      other_team_key = schedule[week_idx_wrap]
      win_i, loss_i, draw_i = self.compute_h2h_result(team_key, other_team_key, week_num)
      n_win += win_i
      n_loss += loss_i
      n_draw += draw_i
    return n_win, n_loss, n_draw


def print_records_dict(d, nweeks):
  # first print all keys
  print('all nonzero records in dict:')
  for k in sorted(d.keys()):
    print(f'{k}: {d[k]}')
  # then print all possible w/l records assuming zero ties:
  print('all possible w/l records assuming no ties:')
  for nwins in range(nweeks + 1):
    nloss = nweeks - nwins
    k = (nwins, nloss, 0)
    print(f'{k}: {d[k]}')


def get_week_score(scores_df, team_key, week):
  return scores_df.loc[(team_key, week)]['points']


def permute_schedules(teamid, allteamids):
  """
  Return an iterable giving all permutations of a list of n teams, excluding teamid.

  Args:
    teamid (int or str): id of the team we are generating the schedule for
    allteamids (list or iterable object): list of all teamids in the league

  Returns:

  """
  opponents = list(allteamids)
  opponents.remove(teamid)
  return itertools.permutations(opponents)


def get_parser():
  parser = argparse.ArgumentParser()
  parser.add_argument('--league', '-l', default=None)
  parser.add_argument('--weeks', '-n', default=None, type=int)
  return parser


def yahoo_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
  """Create and return argument parser."""
  if parser is None:
    parser = argparse.ArgumentParser()
  parser.add_argument('--league_id', type=str)
  parser.add_argument('--client_id', type=str)
  parser.add_argument('--client_secret', type=str)
  parser.add_argument('--redirect_uri', type=str, default='oob')
  parser.add_argument('--force_refresh_token', action='store_true')
  return parser


if __name__ == '__main__':
  main()


# todos:
# implement logging by importing same settings from yahoo module
# maybe replace use of 'warnings' module with logger.warn messages
# implement full schedule generation in addition to
# for better efficiency, maybe organize team scores into a dataframe first instead of querying from each team each time
# implement loading private credentials from file instead of passing by argument