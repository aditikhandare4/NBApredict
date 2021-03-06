"""schedule.py contains function to create the schedule table in the database"""

from datetime import datetime, timedelta
import math
import nbapredict.management.conversion as convert
from sqlalchemy import ForeignKey, func, tuple_
from sqlalchemy.orm import aliased
import pandas as pd


def format_data(session, schedule_data, team_tbl, team_stats_tbl):
    """Format and return schedule data to match the database schema.

    Adds a Margin of Victory column and adds/modifies foreign key columns

    Args:
        schedule_data: A DataOperator object with schedule data
        team_tbl: A mapped instance of the team_tbl
        team_stats_tbl: A mapped instance of the team_stats_tbl
    """
    h_score = schedule_data.data['home_team_score']
    a_score = schedule_data.data['away_team_score']
    schedule_data.data['MOV'] = [h_score[i] - a_score[i] for i in range(schedule_data.num_rows())]
    schedule_data.data['playoffs'] = ['']
    schedule_data.data['game_date'] = [datetime.date(t) for t in schedule_data.data['start_time']]
    schedule_data.fill('playoffs', None)
    schedule_data.data["home_team_id"] = convert.values_to_foreign_key(session, foreign_tbl=team_tbl, foreign_key="id",
                                                                       foreign_value="team_name",
                                                                       child_data=schedule_data.data.pop("home_team"))
    schedule_data.data["away_team_id"] = convert.values_to_foreign_key(session, foreign_tbl=team_tbl, foreign_key="id",
                                                                       foreign_value="team_name",
                                                                       child_data=schedule_data.data.pop("away_team"))

    today = datetime.date(datetime.now())
    tomorrow = today + timedelta(days=1)
    tmrw_idx = 0
    for idx in range(len(schedule_data.data['start_time'])):
        if schedule_data.data['start_time'][idx].date() >= tomorrow:
            tmrw_idx = idx
            break
    if not tmrw_idx:
        raise ValueError("tmrw_idx was not found")
    subquery = session.query(team_stats_tbl.id, team_stats_tbl.team_id, func.max(team_stats_tbl.scrape_time)). \
        filter(team_stats_tbl.scrape_date <= today).group_by(team_stats_tbl.team_id).subquery()
    schedule_data.data['home_stats_id'] = convert.values_to_foreign_key(session, subquery, 'id', 'team_id',
                                                                        schedule_data.data['home_team_id'][:tmrw_idx])
    schedule_data.data['away_stats_id'] = convert.values_to_foreign_key(session, subquery, 'id', 'team_id',
                                                                        schedule_data.data['away_team_id'][:tmrw_idx])
    schedule_data.fill('home_stats_id', None)
    schedule_data.fill('away_stats_id', None)

    return schedule_data


def create_table(db, schedule_data, tbl_name, team_tbl, team_stats_tbl):
    """Create a table of the NBA schedule in the database.
    Args:
        db: a datotable.database.Database object connected to a database
        schedule_data: A datatotable.data.DataOperator object with schedule data
        tbl_name: The desired name of the table
        team_tbl: A mapped team table to set foreign keys on
        team_stats_tbl: A mapped team stats table to set foreign keys on
    """
    columns = schedule_data.columns
    team_tbl_name = team_tbl.__table__.fullname
    team_stats_tbl_name = team_stats_tbl.__table__.fullname
    columns['home_team_id'].append(ForeignKey("{}.id".format(team_tbl_name)))
    columns['away_team_id'].append(ForeignKey("{}.id".format(team_tbl_name)))
    columns['home_stats_id'].append(ForeignKey("{}.id".format(team_stats_tbl_name)))
    columns['away_stats_id'].append(ForeignKey("{}.id".format(team_stats_tbl_name)))
    db.map_table(tbl_name=tbl_name, columns=columns)
    db.create_tables()
    db.clear_mappers()


def update_table(session, schedule_data, schedule_tbl, team_stats_tbl):
    """Wrap and run update functions for the schedule_tbl."""

    update_games(session, schedule_tbl, schedule_data)
    score_updates = update_scores(session, schedule_tbl, schedule_data)
    stats_updates = update_stats(session, schedule_tbl, team_stats_tbl)
    time_updates = update_start_time(session, schedule_tbl, schedule_data)

    # Some rows may be updated in different functions. Use a set to remove duplicates
    return set(score_updates + stats_updates + time_updates)


def update_scores(session, schedule_tbl, schedule_data) -> list:
    date = datetime.date(datetime.now())
    update_query = session.query(schedule_tbl).filter(schedule_tbl.start_time < date,
                                                      schedule_tbl.home_team_score == 0). \
        order_by(schedule_tbl.start_time)
    # if update_query.count() == 0:
    #     return
    rows = update_query.all()
    if len(rows) == 0:
        return []
    first_game_time = rows[0].start_time
    last_game_time = rows[len(rows) - 1].start_time

    sched_df = schedule_data.dataframe
    sched_df["start_time"] = sched_df["start_time"].dt.tz_localize(None)
    update_df = sched_df.loc[(sched_df.start_time >= first_game_time) & (sched_df.start_time <= last_game_time)]

    update_rows = []
    for row in rows:
        game = update_df.loc[(update_df.home_team_id == row.home_team_id) & (update_df.away_team_id == row.away_team_id)
                             & (update_df.start_time.dt.date == datetime.date(row.start_time))]
        row.home_team_score = int(game.home_team_score)
        row.away_team_score = int(game.away_team_score)
        row.MOV = row.home_team_score - row.away_team_score
        row.start_time = game.start_time.dt.to_pydatetime()[0]  # Convert Pandas TimeStamp to datetime
        update_rows.append(row)
    return update_rows


def update_stats(session, schedule_tbl, team_stats_tbl) -> list:
    tomorrow = datetime.date(datetime.now()) + timedelta(days=1)

    d_time = session.query(func.min(schedule_tbl.start_time)).filter(schedule_tbl.home_stats_id == None).all()[0][0]
    date = datetime.date(d_time)
    date_ranges = []
    while date < tomorrow:
        next_day = date + timedelta(days=1)
        date_ranges.append((date, next_day))
        date = next_day

    update_rows = []
    for d in date_ranges:
        # Get the team stats with the greatest scrape_time before the end date of the range (31 obs, all teams + L. AVG)
        stats_q = session.query(team_stats_tbl.id, team_stats_tbl.team_id,
                                func.max(team_stats_tbl.scrape_time).label('s_time')). \
            filter(team_stats_tbl.scrape_time < d[1]).group_by(team_stats_tbl.team_id).subquery()
        home_stats = aliased(stats_q, 'home_stats')
        away_stats = aliased(stats_q, 'away_stats')

        sched_rows = session.query(schedule_tbl, home_stats.c.id.label('h_s_id'), away_stats.c.id.label('a_s_id')). \
            filter(schedule_tbl.home_stats_id == None, schedule_tbl.start_time > d[0], schedule_tbl.start_time < d[1]).\
            join(home_stats, schedule_tbl.home_team_id == home_stats.c.team_id). \
            join(away_stats, schedule_tbl.away_team_id == away_stats.c.team_id).all()

        # ToDo: remove explicit 2020 references
        for row in sched_rows:
            row.schedule_2020.home_stats_id = row.a_s_id
            row.schedule_2020.away_stats_id = row.h_s_id
            update_rows.append(row.schedule_2020)
    return update_rows


def update_start_time(session, schedule_tbl, schedule_data) -> list:
    """Return updated rows for any games where the start_time has changed.

    Note this will not check if the date of a game has changed."""
    today = datetime.date(datetime.now())
    end_week = datetime.date(datetime.now()) + timedelta(days=7)

    games = session.query(schedule_tbl).filter(schedule_tbl.game_date >= today,
                                                    schedule_tbl.game_date <= end_week).all()

    df = schedule_data.dataframe[['start_time', 'game_date', 'home_team_id', 'away_team_id']]
    df.start_time = df.start_time.dt.tz_localize(None)
    df = df[(df.start_time >= pd.Timestamp(today)) & (df.game_date <= end_week)]

    update_rows = []
    for game in games:
        if df[(df.start_time == game.start_time) & (df.home_team_id == game.home_team_id)].empty:
            date = game.game_date
            changed_game = df[(df.home_team_id == game.home_team_id) & (df.away_team_id == game.away_team_id) &
                              (df.game_date == game.game_date)]
            if changed_game.empty:
                raise ValueError('Game time for {} @ {} has changed,'
                                 ' but cannot find the new game time'.format(game.home_team_id,
                                                                             game.away_team_id))
            elif len(changed_game) == 1:
                new_time = changed_game.start_time
                new_time_timestamp = pd.to_datetime(new_time.values[0])
                game.start_time = new_time_timestamp
                update_rows.append(game)
            else:
                raise ValueError('Game time for {} @ {} has changed,'
                                 'but there are multiple replacement values available'.format(game.home_team_id,
                                                                                              game.away_team_id))
    return update_rows


def update_games(session, schedule_tbl, schedule_data):
    """Check if any games have been removed or added from the schedule and add that change to the database.

    ToDo: Add check for new games (i.e. when Clippers-Lakers gets rescheduled)
    ToDo: This should work for playoff games too, right?
    ToDo: Iterating through indices potentially slow, though great alternatives don't seem to exist
    """

    data_len = len(schedule_data.data['start_time'])
    tbl_len = session.query(schedule_tbl).count()
    if data_len < tbl_len:
        data_df = pd.DataFrame({'home_team_id': schedule_data.data['home_team_id'],
                                'game_date': schedule_data.data['game_date']})

        tbl_id_dates = session.query(schedule_tbl.home_team_id, schedule_tbl.game_date).all()
        id_dates_dict = {'home_team_id': [r.home_team_id for r in tbl_id_dates],
                         'game_date': [r.game_date for r in tbl_id_dates]}
        tbl_df = pd.DataFrame(id_dates_dict)

        # Outer join for all rows, indicator for diff column
        comp = data_df.merge(tbl_df, how='outer', indicator=True)
        tbl_only = comp[comp['_merge'] == 'right_only']
        ids = tbl_only['home_team_id'].values.tolist()
        dates = tbl_only['game_date'].values.tolist()
        cancelled_games = [(ids[i], dates[i]) for i in range(len(ids))]

        delete_rows = session.query(schedule_tbl).filter(tuple_(schedule_tbl.home_team_id, schedule_tbl.game_date).
                                                         in_(cancelled_games))
        if delete_rows.count() > 0:
            for row in delete_rows:
                session.delete(row)


