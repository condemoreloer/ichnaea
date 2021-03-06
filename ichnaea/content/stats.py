from calendar import timegm
from collections import defaultdict
from datetime import date, timedelta
from operator import itemgetter

import iso3166
import mobile_codes
from sqlalchemy import func

from ichnaea.models import (
    Cell,
    Radio,
)

from ichnaea.models.content import (
    Score,
    ScoreKey,
    Stat,
    StatKey,
    User,
)
from ichnaea import util

transliterate_mapping = {
    197: 'A', 229: 'a', 231: 'c', 233: 'e', 244: 'o',
}


def transliterate(string):
    # optimize for the common case of ascii-only
    non_ascii = any([ord(c) > 127 for c in string])
    if not non_ascii:
        return string

    result = []
    for c in string:
        if ord(c) > 127:
            result.append(transliterate_mapping.get(ord(c), c))
        else:
            result.append(c)

    return ''.join(result)


def global_stats(session):
    today = util.utcnow().date()
    yesterday = today - timedelta(1)
    stat_keys = (
        StatKey.cell,
        StatKey.wifi,
        StatKey.unique_cell,
        StatKey.unique_ocid_cell,
        StatKey.unique_wifi,
    )
    rows = session.query(Stat.key, Stat.value).filter(
        Stat.key.in_(stat_keys)).filter(
        Stat.time == yesterday)

    stats = {}
    for row in rows.all():
        if row[1]:
            stats[row[0]] = int(row[1])

    result = {}
    for stat_key in stat_keys:
        name = stat_key.name
        try:
            result[name] = stats[stat_key]
        except KeyError:
            # no stats entry available, maybe closely after midnight
            # and task hasn't run yet, take latest value
            row = session.query(Stat.value).filter(
                Stat.key == stat_key).order_by(
                Stat.time.desc()).limit(1).first()
            if row is not None:
                result[name] = row[0]
            else:
                result[name] = 0

    for k, v in result.items():
        # show as millions
        result[k] = '%.2f' % ((v // 10000) / 100.0)

    return result


def histogram(session, stat_key, days=365):
    today = util.utcnow().date()
    start = today - timedelta(days=days)
    month_key = (func.year(Stat.time), func.month(Stat.time))
    rows = session.query(func.max(Stat.value), *month_key).filter(
        Stat.key == stat_key).filter(
        Stat.time >= start).filter(
        Stat.time < today).group_by(
        *month_key).order_by(
        *month_key
    )
    result = []
    for num, year, month in rows.all():
        # use first of August to plot the highest result for July
        if month == 12:  # pragma: no cover
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        if next_month >= today:
            # we restrict dates to be at most yesterday
            next_month = today - timedelta(days=1)
        day = timegm(next_month.timetuple()) * 1000
        result.append([day, num])
    return [result]


def leaders(session):
    score_rows = session.query(
        Score.userid, func.sum(Score.value)).filter(
        Score.key == ScoreKey.location).group_by(
        Score.userid).having(func.sum(Score.value) >= 10).all()
    # sort descending by value
    score_rows.sort(key=itemgetter(1), reverse=True)
    userids = [s[0] for s in score_rows]
    if not userids:
        return []
    user_rows = session.query(User.id, User.nickname).filter(
        User.id.in_(userids)).all()
    users = dict(user_rows)

    result = []
    for userid, value in score_rows:
        nickname = users.get(userid, 'anonymous')
        if len(nickname) > 24:
            nickname = nickname[:24] + u'...'
        result.append(
            {'nickname': nickname, 'num': int(value)})
    return result


def leaders_weekly(session, batch=20):
    result = {'new_cell': [], 'new_wifi': []}
    today = util.utcnow().date()
    one_week = today - timedelta(7)

    score_rows = {}
    userids = set()
    for name in ('new_cell', 'new_wifi'):
        score_rows[name] = session.query(
            Score.userid, func.sum(Score.value)).filter(
            Score.key == ScoreKey[name]).filter(
            Score.time >= one_week).order_by(
            func.sum(Score.value).desc()).group_by(
            Score.userid).limit(batch).all()
        userids.update(set([s[0] for s in score_rows[name]]))

    if not userids:  # pragma: no cover
        return result

    user_rows = session.query(User.id, User.nickname).filter(
        User.id.in_(userids)).all()
    users = dict(user_rows)

    for name, value in score_rows.items():
        for userid, value in value:
            nickname = users.get(userid, 'anonymous')
            if len(nickname) > 24:  # pragma: no cover
                nickname = nickname[:24] + u'...'
            result[name].append(
                {'nickname': nickname, 'num': int(value)})

    return result


def countries(session):
    # We group by radio, mcc to take advantage of the index
    # and explicitly specify a small list of all valid radio values
    # to get mysql to actually use the index.
    radios = set([radio for radio in Radio])
    rows = session.query(Cell.radio, Cell.mcc, func.count()).filter(
        Cell.radio.in_(radios)).group_by(Cell.radio, Cell.mcc).all()

    # reverse grouping by mcc, radio
    mccs = defaultdict(dict)
    for row in rows:
        mccs[row.mcc][row.radio] = row[2]

    countries = {}
    for mcc, item in mccs.items():
        iso_codes = [rec.alpha2 for rec in mobile_codes.mcc(str(mcc))]
        multiple = bool(len(iso_codes) > 1)
        for alpha2 in iso_codes:
            name = iso3166.countries_by_alpha2[alpha2].apolitical_name
            country = {
                'code': alpha2,
                'name': name,
                'order': transliterate(name[:10].lower()),
                'multiple': multiple,
                'total': 0,
                'gsm': 0, 'cdma': 0, 'umts': 0, 'lte': 0,
            }
            for radio, value in item.items():
                country[radio.name] = int(value)
            country['total'] = int(sum(item.values()))
            if alpha2 not in countries:
                countries[alpha2] = country
            else:
                # some countries like the US have multiple mcc codes,
                # we merge them here
                for radio_name, value in country.items():
                    if isinstance(value, int):
                        countries[alpha2][radio_name] += value

    return sorted(countries.values(), key=itemgetter('name'))
