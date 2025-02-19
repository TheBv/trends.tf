# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2020-21 Sean Anderson <seanga2@gmail.com>

import flask
import sqlite3

from common import get_filters, get_order
from sql import get_db
from steamid import SteamID

root = flask.Blueprint('root', __name__)

@root.route('/')
def index():
    c = get_db()
    logstat = c.cursor()
    logstat.execute(
        """SELECT
               count(*) AS count,
               max(time) AS newest,
               min(time) AS oldest
           FROM log;""")
    players = c.cursor()
    players.execute("SELECT count(*) AS players FROM player;")
    return flask.render_template("index.html", logstat=logstat.fetchone(),
                                 players=players.fetchone()[0])

@root.route('/search')
def search():
    args = flask.request.args
    limit = args.get('limit', 25, int)
    offset = args.get('offset', 0, int)
    q = args.get('q', '', str)

    try:
        steamid = SteamID(q)
        cur = get_db().cursor()
        cur.execute(
            """SELECT steamid64
               FROM player_stats
               WHERE steamid64 = %s
               LIMIT 1""", (steamid,))
        for (steamid,) in cur:
            return flask.redirect(flask.url_for('player.overview', steamid=steamid), 302)
    except ValueError:
        pass

    error = None
    results = []
    if len(q) >= 3:
        results = get_db().cursor()
        results.execute(
            """SELECT
                   steamid64,
                   name,
                   avatarhash,
                   aliases
               FROM (SELECT
                       steamid64,
                       array_agg(DISTINCT name) AS aliases,
                       max(rank) AS rank
                   FROM (SELECT
                           steamid64,
                           name,
                           ts_rank(name_vector, query) AS rank
                       FROM (SELECT
                               nameid,
                               name,
                               to_tsvector('english', name) AS name_vector
                           FROM name
                       ) AS name
                       JOIN (SELECT
                               phraseto_tsquery('english', %s) AS query
                       ) AS query ON (TRUE)
                       JOIN player_stats USING (nameid)
                       WHERE query @@ name_vector
                       ORDER BY rank DESC
                   ) AS matches
                   GROUP BY steamid64
               ) AS matches
               JOIN player USING (steamid64)
               JOIN name USING (nameid)
               ORDER BY rank DESC, last_active DESC
               LIMIT %s OFFSET %s;""", (q, limit, offset))
        results = results.fetchall()
    else:
        error = "Searches must contain at least 3 characters"
    return flask.render_template("search.html", q=q, results=results, error=error,
                                 offset=offset, limit=limit)

@root.route('/leaderboard')
def leaderboard():
    limit = flask.request.args.get('limit', 100, int)
    offset = flask.request.args.get('offset', 0, int)
    filters = get_filters(flask.request.args)
    order, order_clause = get_order(flask.request.args, {
        'duration': "duration",
        'logs': "logs",
        'winrate': "winrate",
        'rating': "rating",
    }, 'rating')

    db = get_db()
    ids = db.cursor()
    ids.execute("""SELECT
                       (SELECT
                               classid
                           FROM class
                           WHERE class = %(class)s
                       ) AS classid,
                       (SELECT
                               formatid
                           FROM format
                           WHERE format = %(format)s
                       ) AS formatid;""", filters)
    ids = ids.fetchone()
    leaderboard = db.cursor()
    leaderboard.execute("""SELECT
                               name,
                               avatarhash,
                               steamid64,
                               duration,
                               logs,
                               winrate,
                               rating
                           FROM (SELECT
                                   steamid64,
                                   sum(duration) AS duration,
                                   sum(wins + losses + ties) AS logs,
                                   sum(0.5 * ties + wins) /
                                       sum(wins + losses + ties) AS winrate,
                                   (50 + sum(0.5 * ties + wins)) /
                                       (100 + sum(wins + losses + ties)) AS rating
                               FROM leaderboard_cube
                               LEFT JOIN map USING (mapid)
                               WHERE steamid64 NOTNULL
                                   AND (classid = %(classid)s
                                       OR (%(classid)s ISNULL AND classid ISNULL))
                                   AND (formatid = %(formatid)s
                                       OR (%(formatid)s ISNULL AND formatid ISNULL))
                                   AND (map ILIKE %(map)s
                                       OR (%(map)s ISNULL AND map ISNULL))
                               GROUP BY steamid64
                               ORDER BY {} NULLS LAST
                               LIMIT %(limit)s OFFSET %(offset)s
                           ) AS leaderboard
                           LEFT JOIN player USING (steamid64)
                           LEFT JOIN name USING (nameid);""".format(order_clause),
                           { **filters, **ids, 'limit': limit, 'offset': offset })
    return flask.render_template("leaderboard.html", leaderboard=leaderboard.fetchall(),
                                 filters=filters, order=order, offset=offset, limit=limit)

@root.route('/log')
def log():
    db = get_db()
    logids = flask.request.args.getlist('id', int)
    if not logids:
        flask.abort(404)
    elif len(logids) > 5:
        flask.abort(400)

    logs = db.cursor()
    logs.execute("""WITH RECURSIVE logs_full AS (
                        SELECT
                            *,
                            logid AS original_logid
                        FROM log
                        WHERE logid IN %s
                        UNION ALL
                        SELECT
                            log.*,
                            logs_full.original_logid
                        FROM logs_full
                        JOIN log ON (log.logid = logs_full.duplicate_of)
                    ) SELECT
                        logid,
                        original_logid,
                        time,
                        title,
                        map,
                        format,
                        duration,
                        red_score,
                        blue_score
                    FROM logs_full
                    JOIN format USING (formatid)
                    JOIN map USING (mapid)
                    WHERE duplicate_of ISNULL
                    ORDER BY array_position(%s, original_logid);""", (tuple(logids), logids))
    logs = logs.fetchall()
    logids = tuple(log['logid'] for log in logs)
    if not logids:
        flask.abort(404)

    rounds = db.cursor()
    rounds.execute("""SELECT
                          logid,
                          seq,
                          duration,
                          red_score,
                          blue_score,
                          red_kills,
                          blue_kills,
                          red_dmg,
                          blue_dmg,
                          red_dmg * 60.0 / nullif(duration, 0) AS red_dpm,
                          blue_dmg * 60.0 / nullif(duration, 0) AS blue_dpm,
                          red_ubers,
                          blue_ubers
                      FROM round
                      JOIN round_extra USING (logid, seq)
                      WHERE logid IN %s;""", (logids,))

    players = db.cursor()
    players.execute(
        """SELECT
               players.*,
               heal_stats.healing,
               healing * 60.0 / nullif(duration, 0) AS hpm,
               avatarhash
           FROM (SELECT
                   steamid64,
                   json_object_agg(logid, teamid) AS teamids,
                   json_object_agg(logid, team) AS teams,
                   array_agg(DISTINCT name ORDER BY name) AS names,
                   sum(kills) AS kills,
                   sum(deaths) AS deaths,
                   sum(assists) AS assists,
                   sum(dmg) AS dmg,
                   sum(dt) AS dt,
                   sum(dmg) * 60.0 / sum(duration) AS dpm,
                   sum(dt) * 60.0 / sum(duration) AS dtm,
                   sum(duration) AS duration,
                   max(lks) AS lks,
                   total(airshots) AS airshots,
                   total(medkits) AS medkits,
                   total(medkits_hp) AS medkits_hp,
                   total(backstabs) AS backstabs,
                   total(headshots) AS headshots,
                   total(headshots_hit) AS headshots_hit,
                   total(sentries) AS sentries,
                   total(cpc) AS cpc,
                   total(ic) AS ic
               FROM log
               JOIN player_stats AS ps USING (logid)
               LEFT JOIN player_stats_extra AS pse USING (logid, steamid64)
               LEFT JOIN heal_stats_received AS hsr USING (logid, steamid64)
               JOIN team USING (teamid)
               JOIN name USING (nameid)
               WHERE logid IN %s
               GROUP BY steamid64
               ORDER BY array_agg(teamid ORDER BY logid), array_agg(DISTINCT name)
           ) AS players
           JOIN player USING (steamid64)
           LEFT JOIN (SELECT
                    healee AS steamid64,
                    total(healing) AS healing
                FROM heal_stats
                WHERE logid IN %s
                GROUP BY healee
           ) AS heal_stats USING (steamid64);""", (logids, logids))
    players=players.fetchall()

    # This is difficult to do in SQL, since we don't have any rows for players who didn't play in a
    # log but still played in another log. So instead we do it in python.
    def player_key(player):
        # 500 is probably greater than any teamid :)
        teams = (player['teamids'].get(str(logid), 500) for logid in logids)
        names = player.get('names', ())
        return (*teams, *names)
    players.sort(key=player_key)
    players = { player['steamid64']: player for player in players }

    classes = db.cursor()
    classes.execute("""SELECT
                           classes.*,
                           class,
                           classes.duration * 1.0 / logs.duration AS pct,
                           logs.duration AS tot_duration
                       FROM (SELECT
                               steamid64,
                               classid,
                               sum(duration) AS duration,
                               sum(kills) AS kills,
                               sum(deaths) AS deaths,
                               sum(assists) AS assists,
                               sum(dmg) AS dmg,
                               sum(dmg) * 60.0 / sum(duration) AS dpm
                           FROM class_stats
                           WHERE logid IN %s
                           GROUP BY steamid64, classid
                       ) AS classes
                       JOIN (SELECT
                              steamid64,
                              sum(duration) AS duration
                           FROM player_stats
                           JOIN log USING (logid)
                           WHERE LOGID IN %s
                           GROUP BY steamid64
                       ) AS logs USING (steamid64)
                       JOIN class USING (classid)
                       ORDER BY classes.duration DESC;""", (logids, logids))

    weapons = db.cursor()
    weapons.execute("""SELECT
                           steamid64,
                           class,
                           weapon,
                           kills,
                           dmg,
                           shots,
                           hits,
                           hits * 1.0 / nullif(shots, 0) AS acc,
                           dmg * 1.0 / nullif(shots, 0) AS dps
                       FROM (SELECT
                               steamid64,
                               classid,
                               weaponid,
                               sum(kills) AS kills,
                               sum(dmg) AS dmg,
                               sum(shots) AS shots,
                               sum(hits) AS hits
                           FROM weapon_stats
                           WHERE logid IN %s
                           GROUP BY steamid64, classid, weaponid
                           ) AS weapons
                       JOIN class USING (classid)
                       JOIN weapon USING (weaponid);""", (logids,))

    # This query could be constructed based on the results of the above queries, but for now it is
    # done separately to aid development
    totals = db.cursor()
    totals.execute("""SELECT
                          *
                      FROM (SELECT
                              logid,
                              teamid,
                              sum(kills) AS kills,
                              sum(deaths) AS deaths,
                              sum(assists) AS assists,
                              sum(dmg) AS dmg,
                              sum(dt) AS dt,
                              total(hsr.healing) AS healing,
                              sum(dmg) * 60.0 / sum(duration) AS dpm,
                              sum(dt) * 60.0 / sum(duration) AS dtm,
                              total(hsr.healing) * 60.0 / sum(duration) AS hpm,
                              max(lks) AS lks,
                              total(airshots) AS airshots,
                              total(medkits) AS medkits,
                              total(medkits_hp) AS medkits_hp,
                              total(backstabs) AS backstabs,
                              total(headshots) AS headshots,
                              total(headshots_hit) AS headshots_hit,
                              total(sentries) AS sentries,
                              total(cpc) AS cpc,
                              total(ic) AS ic
                          FROM log
                          JOIN player_stats USING (logid)
                          LEFT JOIN player_stats_extra USING (logid, steamid64)
                          LEFT JOIN (SELECT
                                  healee AS steamid64,
                                  sum(healing) AS healing
                              FROM heal_stats
                              WHERE logid IN %s
                              GROUP BY healee
                          ) hsr USING (steamid64)
                          WHERE logid IN %s
                          GROUP BY logid, teamid
                          ORDER BY array_position(%s, logid), teamid
                      ) AS totals
                      JOIN team USING (teamid);""", (logids, logids, list(logids)));

    medics = db.cursor()
    medics.execute("""SELECT
                          medic_stats.*,
                          heal_stats.*,
                          healing * 60.0 / nullif(duration, 0) AS hpm
                      FROM (SELECT
                             json_object_agg(logid, teamid) AS teamids,
                             json_object_agg(logid, team) AS teams,
                             steamid64,
                             sum(coalesce(cs.duration, log.duration)) AS duration,
                             sum(ubers) AS ubers,
                             sum(medigun_ubers) AS medigun_ubers,
                             sum(kritz_ubers) AS kritz_ubers,
                             sum(other_ubers) AS other_ubers,
                             sum(drops) AS drops,
                             sum(advantages_lost) AS advantages_lost,
                             max(biggest_advantage_lost) AS biggest_advantage_lost,
                             sum(deaths_after_uber) AS deaths_after_uber,
                             sum(deaths_before_uber) AS deaths_before_uber
                          FROM medic_stats
                          JOIN player_stats USING (logid, steamid64)
                          JOIN team USING (teamid)
                          JOIN log USING (logid)
                          CROSS JOIN class
                          LEFT JOIN class_stats AS cs USING (logid, steamid64, classid)
                          WHERE logid IN %s
                              AND class = 'medic'
                          GROUP BY steamid64
                      ) AS medic_stats
                      LEFT JOIN (SELECT
                              healer AS steamid64,
                              sum(healing) AS healing,
                              array_agg(json_build_object(
                                  'steamid64', healee,
                                  'healing', healing,
                                  'hpm', healing * 60.0 / nullif(duration, 0),
                                  'duration', duration,
                                  'classes', classes,
                                  'class_pcts', (SELECT
                                                     array_agg(duration * 1.0 / hs.duration)
                                                 FROM unnest(class_durations) AS duration)
                              ) ORDER BY healing DESC) AS healees
                          FROM (SELECT
                                  healer,
                                  healee,
                                  sum(healing) AS healing,
                                  sum(duration) AS duration,
                                  array_agg(class ORDER BY duration DESC) AS classes,
                                  array_agg(duration ORDER BY duration DESC) AS class_durations
                              FROM (SELECT
                                      healer,
                                      healee,
                                      classid,
                                      sum(duration) AS duration,
                                      sum(healing) AS healing
                                  FROM heal_stats AS hs
                                  JOIN class_stats AS cs ON (
                                      hs.logid = cs.logid
                                      AND hs.healee = cs.steamid64
                                  ) WHERE hs.logid in %s
                                  GROUP BY healer, healee, classid
                              ) AS hs
                              JOIN class USING (classid)
                              GROUP BY healer, healee
                          ) AS hs
                          GROUP BY healer
                      ) AS heal_stats USING (steamid64);""", (logids, logids));
    medics = medics.fetchall()
    medics.sort(key=player_key)

    events = db.cursor()
    events.execute("""SELECT
                          event,
                          array_agg(json_build_object(
                              'steamid64', steamid64,
                              'demoman', demoman,
                              'engineer', engineer,
                              'heavyweapons', heavyweapons,
                              'medic', medic,
                              'pyro', pyro,
                              'scout', scout,
                              'sniper', sniper,
                              'soldier', soldier,
                              'spy', spy,
                              'total', total
                          ) ORDER BY total DESC) AS events
                      FROM (SELECT
                              eventid,
                              steamid64,
                              sum(demoman) AS demoman,
                              sum(engineer) AS engineer,
                              sum(heavyweapons) AS heavyweapons,
                              sum(medic) AS medic,
                              sum(pyro) AS pyro,
                              sum(scout) AS scout,
                              sum(sniper) AS sniper,
                              sum(soldier) AS soldier,
                              sum(spy) AS spy,
                              sum(demoman) + sum(engineer) + sum(heavyweapons) + sum(medic)
                                  + sum(pyro) + sum(scout) + sum(sniper) + sum(soldier) + sum(spy)
                                  AS total
                          FROM event_stats
                          WHERE logid IN %s
                          GROUP BY eventid, steamid64
                      ) AS events
                      JOIN event USING (eventid)
                      GROUP BY event;""", (logids,))
    events = { event_stats['event']: event_stats['events'] for event_stats in events.fetchall() }

    chats = db.cursor()
    chats.execute("""SELECT
                        logid,
                        title,
                        array_agg(json_build_object(
                            'team', team,
                            'steamid64', steamid64,
                            'name', name,
                            'msg', msg
                        ) ORDER BY seq) AS messages
                    FROM (SELECT
                            logid,
                            seq,
                            team,
                            steamid64,
                            coalesce(name, 'Console') AS name,
                            msg
                        FROM chat
                        LEFT JOIN player_stats USING (logid, steamid64)
                        LEFT JOIN name USING (nameid)
                        LEFT JOIN team USING (teamid)
                        WHERE logid IN %s
                    ) AS chat
                    JOIN log USING (logid)
                    GROUP BY logid, title
                    ORDER BY array_position(%s, logid);""", (logids, list(logids)))

    return flask.render_template("log.html", logids=logids, logs=logs, rounds=rounds.fetchall(),
                                 players=players, classes=classes.fetchall(),
                                 weapons=weapons.fetchall(), totals=totals, medics=medics,
                                 events=events, chats=chats)
