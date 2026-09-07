"""
App Growth Dashboard — Auto Update Script
Queries BigQuery directly. No Claude/Anthropic API. $0 token cost.
Cost data is managed manually via the Budget tab in the dashboard (localStorage).
"""
import json, os, datetime, calendar
from google.cloud import bigquery

PROJECT = "chotot-dwh"
DATA_JSON = os.path.join(os.path.dirname(__file__), '..', 'data.json')

client = bigquery.Client(project=PROJECT)

def run(sql):
    return [dict(r) for r in client.query(sql).result()]

def to_date(val):
    if isinstance(val, datetime.date): return val
    return datetime.datetime.strptime(str(val)[:10], '%Y-%m-%d').date()

def month_label(d, today):
    label = d.strftime("%b %Y")
    return label + "*" if d >= datetime.date(today.year, today.month, 1) else label

def get_arr(rows, key, months, channel=None):
    lookup = {}
    for r in rows:
        m = to_date(r['month'])
        ch = str(r.get('channel', r.get('channelGrouping', '')))
        if channel is None or ch == channel:
            lookup[m] = r.get(key)
    return [lookup.get(m) for m in months]

def safe_div(a, b):
    return round(a/b, 4) if a and b else None

def daily(arr, days):
    return [round(arr[i]/days[i]) if arr[i] else None for i in range(len(arr))]

# Every section below is wrapped in try/except so one broken query cannot stop
# the other seven from publishing: the handler keeps whatever that section had in
# the previous data.json and the run carries on. The price of that is silence. On
# 2026-08-22 and 08-23 the camp-detail query died on an upstream column rename,
# both runs went green, and section 6 plus the "data đến" badge served 08-19
# figures for two days while every other section moved — nobody was told,
# because nothing had failed.
#
# So each handler now also records itself here, and the workflow turns a
# non-empty list into a failed run in a step that comes *after* the commit. That
# ordering is the whole point: the sections that did work still ship, and the
# failure notification still goes out. Do not make this script exit non-zero
# instead — that runs before the commit and would freeze all eight sections to
# punish one.
SKIPPED_FILE = '/tmp/dashboard_skipped_sections.txt'
SKIPPED = []

def note_skipped(section, err):
    SKIPPED.append(f"{section}: {err}")
    print(f"  WARNING {section} skipped: {err}")
    # GitHub renders this as an annotation on the run page, so the reason is
    # visible without opening the log.
    print(f"::error title=Dashboard section skipped::{section}: {err}")

print("Loading current data.json...")
with open(DATA_JSON) as f:
    D = json.load(f)

print("Querying BigQuery...")
today = datetime.date.today()

# ── Cohort maturity ─────────────────────────────────────────────────────────
# A retention rate with an H-day horizon is structurally zero for the newest H
# days — someone who installed yesterday cannot have a D7 yet. Dividing SUM(dN)
# by SUM(d0) over a running month therefore puts real returners over a
# denominator padded with cohorts that never had the chance to return, and the
# fresher the month the harder it drags. Measured 2026-08-11: August NURR D7 was
# published as 3.8% when the honest figure over matured cohorts is 13.0%, a 3.4x
# understatement on the number people read first. Same cause behind a campaign's
# RR D1 reading 24.3% against a true 30.8% in section 6, which is how this was
# found — someone compared the dashboard with Looker Studio and the two agreed on
# a number that was wrong in both places.
#
# So every rate below is computed only over cohorts old enough to have the
# metric, with the cutoff read from the data rather than assumed: the last day
# the column is actually populated, floored at max_date - horizon so one freak
# zero cannot drag the window backwards. Absolute counts (d0, install, cost)
# keep the full window — they are not cohort metrics, and trimming them would
# make this section contradict the Cost column.
#
# `lead7` is the exception among the counts, because it *is* a cohort metric: it
# counts new users who contacted within 7 days of installing, so a cohort from
# yesterday has only had one of its seven days. Unlike d7 it is not structurally
# zero while it waits — it fills in gradually — which makes it more dangerous,
# not less: the number looks plausible at every moment and simply runs low. It
# therefore gets the same cutoff as the rates, and the cost it is divided by is
# trimmed to match, so Cost/Lead compares the same days on both sides.
#
# Two failure modes have to stay separate, because only one of them is benign:
#   maturity — the window ends at max_date - horizon. Expected, and the partial
#     month is still publishable: its numerator and denominator agree.
#   staleness — the column stopped publishing *earlier* than that. Then the
#     window is short for a reason that has nothing to do with cohort age and
#     the affected months must be blanked, not shown as a decline. `m1` in the
#     activation table has been broken this way since 2026-04-13 (populated
#     04-01..04-12, dead 04-13..05-17, back 05-18..06-15, nothing since) and was
#     publishing Apr 7.4% / May 12.4% / Jun 9.0% against Mar 29.8% — read on the
#     dashboard as a retention collapse that never happened.
MAT_HORIZON = {'d1': 1, 'd7': 7, 'm1': 31, 'lead7': 7}
# Fraction of a month's in-window days that may be missing before the month is
# blanked instead of published. The activation table's d7 has exactly two holes
# (2026-07-07, 2026-07-08 — d7 = 0 while both neighbours are ~1,480, an upstream
# gap, not maturity) and blanking July D7 over 2 days out of 31 would lose more
# than it protects; it understates July by ~6%, so it is flagged instead.
MAT_GAP_TOLERANCE = 0.10
# When a column is stale, a month also has to cover most of itself to be worth
# showing: June m1 has no holes below its cutoff only because the cutoff is
# 06-15, i.e. half the month is simply absent.
MAT_MIN_COVER = 0.80

# lead7 is NULL for ret90, which has no such column — _build_maturity skips a
# metric a source does not carry rather than reporting it as an empty window.
mat_rows = run("""
SELECT src, dt, d1 > 0 AS has_d1, d7 > 0 AS has_d7, m1 > 0 AS has_m1,
       lead7 > 0 AS has_lead7 FROM (
  SELECT 'act' AS src, visit_date AS dt, SUM(d1) AS d1, SUM(d7) AS d7, SUM(m1) AS m1,
         SUM(user_1lead_7d) AS lead7
  FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
  WHERE return_status='new' AND campaign='all' AND channel='all' AND vertical_user='all'
    AND visit_date >= '2026-01-01'
  GROUP BY 1, 2
  UNION ALL
  SELECT 'ret90', min_date, SUM(d1), SUM(d7), SUM(m1), NULL
  FROM ct_digital.dashboard__retention_90d
  WHERE new_status='return' AND min_date >= '2026-01-01'
  GROUP BY 1, 2
)
""")

def _build_maturity(rows):
    """Per (source, metric): the last cohort date whose rate is trustworthy, the
    days missing below it, and whether the column is stale rather than merely
    immature.

    Read at the 'all' grain on purpose. At that grain a zero is unambiguously a
    hole — a day with 30k+ new users cannot have 0 returners — whereas per
    campaign a zero is often the truth, so the same test applied there would
    quietly delete real 0% rows. The calendar derived here is then handed down to
    the per-campaign queries as a date cutoff.
    """
    out = {}
    for src in sorted({str(r['src']) for r in rows}):
        days = sorted((to_date(r['dt']), r) for r in rows if str(r['src']) == src)
        if not days:
            continue
        max_date = days[-1][0]
        for metric, horizon in MAT_HORIZON.items():
            flag = 'has_' + metric
            # A column the source does not carry comes back NULL on every row.
            # That is not an empty window, it is a question that does not apply,
            # so leave the key out entirely: the report stops printing a phantom
            # "ret90.lead7 — no data" line, and anyone who asks for the cutoff
            # anyway falls through mat_through's 1970 guard and gets nothing.
            if all(r[flag] is None for _, r in days):
                continue
            pop = [d for d, r in days if r[flag]]
            mature_end = max_date - datetime.timedelta(days=horizon)
            # No data at all: leave `through` None so callers blank the metric
            # outright instead of dividing by a window that does not exist.
            through = min(max(pop), mature_end) if pop else None
            popset = set(pop)
            gaps = ([d for d, _ in days if d <= through and d not in popset]
                    if through else [])
            out[(src, metric)] = {
                'through': through,
                'gaps': gaps,
                # Strictly earlier than the maturity frontier = the pipeline
                # stopped, not the cohorts being young.
                'stale': bool(through and through < mature_end),
                'max_date': max_date,
            }
    return out

MAT = _build_maturity(mat_rows)

# Every rate on the page now depends on these cutoffs, so a silent failure here
# would not show up as an error — it would show up as a dashboard full of zeros
# and "—", which looks like a bad month rather than a broken script. Refuse to
# write data.json instead: the previous file stays live and CI goes red.
for _src in ('act', 'ret90'):
    _d1 = MAT.get((_src, 'd1'), {}).get('through')
    if _d1 is None:
        raise RuntimeError(
            f"maturity probe returned no usable d1 window for {_src} — refusing to "
            f"rewrite data.json, since every retention rate would be blanked")
    _lag = (today - _d1).days
    if _lag > 10:
        raise RuntimeError(
            f"{_src}.d1 has not published since {_d1} ({_lag} days) — the source "
            f"table has stalled, so the numbers would be stale without saying so")

def mat_through(src, metric):
    """Cutoff as a SQL date literal. Falls back to a date that matches nothing
    when the column is empty, so a broken column yields NULL rates rather than
    silently reverting to the unfiltered (diluted) numbers."""
    t = MAT[(src, metric)]['through'] if (src, metric) in MAT else None
    return (t or datetime.date(1970, 1, 1)).strftime('%Y-%m-%d')

def mat_null(arr, src, metric, months):
    """Blank the months whose window is too short or too holey to publish.

    Applies only to breakage. A month trimmed purely by maturity keeps its value:
    that is the entire point of the cutoff above, and the front end labels the
    date the cohort matured through.
    """
    m = MAT.get((src, metric))
    result = list(arr)
    if not m:
        return result
    through = m['through']
    for i, mth in enumerate(months):
        if i >= len(result):
            break
        if through is None:
            result[i] = None
            continue
        last = min(mth.replace(day=calendar.monthrange(mth.year, mth.month)[1]),
                   m['max_date'])
        win = (min(through, last) - mth).days + 1
        elapsed = (last - mth).days + 1
        gaps = sum(1 for g in m['gaps'] if g.year == mth.year and g.month == mth.month)
        if win <= 0:
            result[i] = None
        elif gaps / win > MAT_GAP_TOLERANCE:
            result[i] = None
        elif m['stale'] and win < MAT_MIN_COVER * elapsed:
            result[i] = None
    return result

for (src, metric), m in sorted(MAT.items()):
    note = ' STALE' if m['stale'] else ''
    if m['gaps']:
        note += ' | %d gap day(s), first %s' % (len(m['gaps']), m['gaps'][0])
    print("  Maturity %s.%s: through %s%s" % (src, metric, m['through'], note))

# MAU
mau_rows = run("""
SELECT month,
  SUM(mau) as mau_app,
  SUM(CASE WHEN login_status='login' THEN mau END) as mau_login
FROM ct_product.dashboard__user_management_login_monthly
WHERE platform IN ('Android','iOS') AND month >= '2026-01-01'
GROUP BY 1 ORDER BY 1
""")

# DAU
dau_rows = run("""
SELECT DATE_TRUNC(date,MONTH) as month, AVG(daily_dau) as avg_dau
FROM (SELECT date, SUM(dau) as daily_dau
FROM ct_product.dashboard__user_management_DAU
WHERE platform IN ('Android','iOS') AND date >= '2026-01-01' GROUP BY date)
GROUP BY 1 ORDER BY 1
""")

# Total CT MAU
ct_rows = run("""
SELECT month, SUM(mau) as total_ct_mau
FROM ct_product.dashboard__user_management_login_monthly
WHERE month >= '2026-01-01' GROUP BY 1 ORDER BY 1
""")

# New users
new_rows = run("""
SELECT DATE_TRUNC(date,MONTH) as month, channelGrouping,
  COUNT(DISTINCT clientId) as new_mau,
  COUNT(DISTINCT CASE WHEN account_id IS NOT NULL THEN clientId END) as new_login_mau
FROM chotot_data.traffic_visit_detail
WHERE newVisits=1 AND platform IN ('iOS','Android') AND date >= '2026-01-01'
GROUP BY 1,2 ORDER BY 1,2
""")
print(f"  MAU: {len(mau_rows)} months | New users: {len(new_rows)} rows")

# Activation (may fail with 403)
act_rows = []
try:
    # Each NURR divides over its own cohort window — see the maturity block up
    # top. The three windows deliberately differ (D1 through max-1, D7 through
    # max-7, M1 through max-31), so the three rates in one month are not over the
    # same denominator and are not meant to be.
    act_rows = run(f"""
    SELECT DATE_TRUNC(visit_date,MONTH) as month,
      CASE WHEN channel='all' THEN 'Total' ELSE channel END as channel,
      AVG(dau) as avg_new_dau,
      SUM(user_20adview_7d) as adview_total, SUM(user_1lead_7d) as lead_total,
      SUM(save_ad) as save_total,
      SAFE_DIVIDE(SUM(IF(visit_date <= DATE '{mat_through('act','d1')}', d1, 0)),
                  SUM(IF(visit_date <= DATE '{mat_through('act','d1')}', d0, 0))) as nurr_d1,
      SAFE_DIVIDE(SUM(IF(visit_date <= DATE '{mat_through('act','d7')}', d7, 0)),
                  SUM(IF(visit_date <= DATE '{mat_through('act','d7')}', d0, 0))) as nurr_d7,
      SAFE_DIVIDE(SUM(IF(visit_date <= DATE '{mat_through('act','m1')}', m1, 0)),
                  SUM(IF(visit_date <= DATE '{mat_through('act','m1')}', d0, 0))) as nurr_m1
    FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
    WHERE return_status='new' AND campaign='all'
    AND vertical_user = 'all'
    AND channel IN ('all','Direct','Organic Search','Paid Search','Display','Growth','Social')
    AND visit_date >= '2026-01-01'
    GROUP BY 1,2 ORDER BY 1,2
    """)
    print(f"  Activation: {len(act_rows)} rows OK")
except Exception as e:
    note_skipped("Activation", e)

# Daily activation — last 90 days by day and channel (for date-range chart)
daily_act_rows = []
try:
    daily_act_rows = run("""
    SELECT visit_date,
      CASE WHEN channel='all' THEN 'Total'
           WHEN channel IN ('Paid Search','Display','Growth') THEN 'Growth'
           ELSE channel END as channel,
      SUM(d0) as new_users,
      SUM(user_20adview_7d) as adview_activated,
      SUM(user_1lead_7d) as lead_activated,
      SUM(save_ad) as save_activated
    FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
    WHERE return_status='new' AND campaign='all'
      AND vertical_user='all'
      AND channel IN ('all','Direct','Organic Search','Paid Search','Display','Growth')
      AND visit_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
    GROUP BY 1,2 ORDER BY 1,2
    """)
    print(f"  Daily activation: {len(daily_act_rows)} rows OK")
except Exception as e:
    note_skipped("Daily activation", e)

# Retention (may fail with 403)
ret_total_rows = []
ret_app_rows = []
ret_web_rows = []
try:
    # Same cohort-window rule as the activation table above, with this table's own
    # cutoffs — they are not the same dates. This one is clean: measured
    # 2026-08-11 it has no missing days at all below its frontier, so the whole
    # correction here is maturity. It moved August D7 from 8.8% to 30.5% and gave
    # July M1 a real 41.2% where the old index-based blanking showed nothing.
    _r90 = lambda metric: (
        f"SAFE_DIVIDE(SUM(IF(min_date <= DATE '{mat_through('ret90', metric)}', {metric}, 0)),"
        f" SUM(IF(min_date <= DATE '{mat_through('ret90', metric)}', d0, 0)))")
    _r90_sel = (f"{_r90('d1')} as ret_d1, {_r90('d7')} as ret_d7, "
                f"{_r90('m1')} as ret_m1")
    ret_total_rows = run(f"""
    SELECT DATE_TRUNC(min_date,MONTH) as month, {_r90_sel}
    FROM ct_digital.dashboard__retention_90d
    WHERE new_status='return' AND min_date >= '2026-01-01'
    GROUP BY 1 ORDER BY 1
    """)
    ret_app_rows = run(f"""
    SELECT DATE_TRUNC(min_date,MONTH) as month, {_r90_sel}
    FROM ct_digital.dashboard__retention_90d
    WHERE new_status='return' AND min_date >= '2026-01-01'
      AND platform IN ('Android','iOS')
    GROUP BY 1 ORDER BY 1
    """)
    ret_web_rows = run(f"""
    SELECT DATE_TRUNC(min_date,MONTH) as month, {_r90_sel}
    FROM ct_digital.dashboard__retention_90d
    WHERE new_status='return' AND min_date >= '2026-01-01'
      AND platform NOT IN ('Android','iOS')
    GROUP BY 1 ORDER BY 1
    """)
    print(f"  Retention: total={len(ret_total_rows)}, app={len(ret_app_rows)}, web={len(ret_web_rows)} rows OK")
except Exception as e:
    note_skipped("Retention", e)
    ret_web_rows = []

# Build month list
all_months = sorted(set(to_date(r['month']) for r in mau_rows))
months_labels = [month_label(m, today) for m in all_months]
partial = [m for m in months_labels if m.endswith("*")]
n = len(all_months)

# Days per month — partial month uses actual days elapsed (today.day - 1)
def days_in(d):
    if d.year == today.year and d.month == today.month:
        return max(1, today.day - 1)
    if d.month == 12: return 31
    return (datetime.date(d.year, d.month+1, 1) - datetime.timedelta(days=1)).day
days = [days_in(m) for m in all_months]

# Overview
mau_app   = get_arr(mau_rows, 'mau_app', all_months)
mau_login = get_arr(mau_rows, 'mau_login', all_months)
ct_mau    = get_arr(ct_rows, 'total_ct_mau', all_months)
avg_dau   = [round(v) if v else None for v in get_arr(dau_rows, 'avg_dau', all_months)]

# New users aggregated
ch_map = {}
for r in new_rows:
    m = to_date(r['month'])
    ch = r.get('channelGrouping','')
    ch_map[(m,ch)] = r

def by_ch(ch, key='new_mau'):
    return [ch_map.get((m,ch),{}).get(key, 0) or 0 for m in all_months]

direct_n  = by_ch('Direct'); organic_n = by_ch('Organic Search')
paid_n    = by_ch('Paid Search'); display_n= by_ch('Display')
growth_crm= by_ch('Growth'); other_n   = by_ch('(Other)')
growth_n  = [paid_n[i]+display_n[i]+growth_crm[i] for i in range(n)]
total_n   = [direct_n[i]+organic_n[i]+growth_n[i]+other_n[i] for i in range(n)]
new_login_total = [
    by_ch('Direct','new_login_mau')[i] + by_ch('Organic Search','new_login_mau')[i] +
    by_ch('Paid Search','new_login_mau')[i] + by_ch('Display','new_login_mau')[i] +
    by_ch('Growth','new_login_mau')[i] + by_ch('(Other)','new_login_mau')[i]
    for i in range(n)]

# Activation / NURR
if act_rows:
    def a(ch, key): return get_arr(act_rows, key, all_months, channel=ch)
    adview_total = a('Total','adview_total'); lead_total = a('Total','lead_total')
    save_total = a('Total','save_total')
    nurr_d1=a('Total','nurr_d1'); nurr_d7=a('Total','nurr_d7'); nurr_m1=a('Total','nurr_m1')
    dir_adv=a('Direct','adview_total'); org_adv=a('Organic Search','adview_total')
    paid_adv=a('Paid Search','adview_total'); disp_adv=a('Display','adview_total')
    crm_adv=a('Growth','adview_total')
    growth_adv=[( paid_adv[i] or 0)+(disp_adv[i] or 0)+(crm_adv[i] or 0) for i in range(n)]
    dir_lead=a('Direct','lead_total'); org_lead=a('Organic Search','lead_total')
    paid_lead=a('Paid Search','lead_total'); disp_lead=a('Display','lead_total')
    crm_lead=a('Growth','lead_total')
    growth_lead=[(paid_lead[i] or 0)+(disp_lead[i] or 0)+(crm_lead[i] or 0) for i in range(n)]
    dir_save=a('Direct','save_total'); org_save=a('Organic Search','save_total')
    paid_save=a('Paid Search','save_total'); disp_save=a('Display','save_total')
    crm_save=a('Growth','save_total')
    growth_save=[(paid_save[i] or 0)+(disp_save[i] or 0)+(crm_save[i] or 0) for i in range(n)]
    dir_d1=a('Direct','nurr_d1'); dir_d7=a('Direct','nurr_d7'); dir_m1=a('Direct','nurr_m1')
    org_d1=a('Organic Search','nurr_d1'); org_d7=a('Organic Search','nurr_d7'); org_m1=a('Organic Search','nurr_m1')
    paid_d1=a('Paid Search','nurr_d1'); paid_d7=a('Paid Search','nurr_d7'); paid_m1=a('Total','nurr_m1')
else:
    print("  Using existing activation data")
    ex=D['activation']; er=D['retention']; eg=D['growth_channel']
    adview_total=ex['adview_total']; lead_total=ex['lead_total']
    save_total=ex.get('save_total',[None]*n)
    nurr_d1=er['nurr_d1']; nurr_d7=er['nurr_d7']; nurr_m1=er['nurr_m1']
    dir_adv=ex['direct_adview']; org_adv=ex['organic_adview']; growth_adv=ex['growth_adview']
    dir_lead=ex['direct_lead']; org_lead=ex['organic_lead']; growth_lead=ex['growth_lead']
    dir_save=ex.get('direct_save',[None]*n); org_save=ex.get('organic_save',[None]*n)
    growth_save=ex.get('growth_save',[None]*n)
    dir_d1=er['direct_d1']; dir_d7=er['direct_d7']; dir_m1=er['direct_m1']
    org_d1=er['organic_d1']; org_d7=er['organic_d7']; org_m1=er['organic_m1']
    paid_d1=eg['nurr_d1']; paid_d7=eg['nurr_d7']; paid_m1=eg['nurr_m1']

if ret_total_rows:
    tot_d1_bq=get_arr(ret_total_rows,'ret_d1',all_months); tot_d7_bq=get_arr(ret_total_rows,'ret_d7',all_months)
    tot_m1_bq=get_arr(ret_total_rows,'ret_m1',all_months)
    app_d1=get_arr(ret_app_rows,'ret_d1',all_months) if ret_app_rows else tot_d1_bq
    app_d7=get_arr(ret_app_rows,'ret_d7',all_months) if ret_app_rows else tot_d7_bq
    app_m1=get_arr(ret_app_rows,'ret_m1',all_months) if ret_app_rows else tot_m1_bq
    web_d1=get_arr(ret_web_rows,'ret_d1',all_months) if ret_web_rows else [None]*n
    web_d7=get_arr(ret_web_rows,'ret_d7',all_months) if ret_web_rows else [None]*n
    web_m1=get_arr(ret_web_rows,'ret_m1',all_months) if ret_web_rows else [None]*n
else:
    er=D['retention']
    tot_d1_bq=er.get('total_d1',[None]*n); tot_d7_bq=er.get('total_d7',[None]*n); tot_m1_bq=er.get('total_m1',[None]*n)
    app_d1=er.get('app_d1',tot_d1_bq); app_d7=er.get('app_d7',tot_d7_bq); app_m1=er.get('app_m1',tot_m1_bq)
    web_d1=er.get('web_d1',[None]*n); web_d7=er.get('web_d7',[None]*n); web_m1=er.get('web_m1',[None]*n)

def pad(arr, length, val=None):
    return list(arr) + [val]*(length-len(arr))

tot_d1 = pad(tot_d1_bq, n)
tot_d7 = pad(tot_d7_bq, n)
tot_m1 = pad(tot_m1_bq, n)

prev_month_i = n - 2     # second-to-last = previous (last full) month

# M1 used to be blanked by position — "null the last two indices" — on the
# assumption that a 30-day window always needs the current and previous month to
# finish. That guessed at the calendar instead of reading the table, and got it
# wrong in both directions: it blanked ret90's July M1, which has ten fully
# matured days and a real 41.2%, while happily publishing the activation table's
# Apr/May/Jun M1, which is broken data (see the maturity block up top) and read on
# the dashboard as a fall from 29.8% to 7.4%. mat_null decides from the column
# itself, so the rates come from cohorts that actually matured and the months it
# cannot stand behind are the ones that go blank.
#
# Applied to every published rate, not just the ones known to be broken today. On
# a healthy column it is a no-op, so the cost of that is nothing and it means the
# next column to go stale gets blanked by itself instead of quietly shipping a
# fake decline until someone notices.
nurr_d1 = nurr_d1 if act_rows else D['retention']['nurr_d1']
nurr_d7 = nurr_d7 if act_rows else D['retention']['nurr_d7']
nurr_m1 = nurr_m1 if act_rows else D['retention']['nurr_m1']

_act_d1 = lambda a: mat_null(a, 'act', 'd1', all_months)
_act_d7 = lambda a: mat_null(a, 'act', 'd7', all_months)
_act_m1 = lambda a: mat_null(a, 'act', 'm1', all_months)
_r90_d1 = lambda a: mat_null(a, 'ret90', 'd1', all_months)
_r90_d7 = lambda a: mat_null(a, 'ret90', 'd7', all_months)
_r90_m1 = lambda a: mat_null(a, 'ret90', 'm1', all_months)

nurr_d1 = _act_d1(nurr_d1);  nurr_d7 = _act_d7(nurr_d7);  nurr_m1 = _act_m1(nurr_m1)
dir_d1  = _act_d1(dir_d1);   dir_d7  = _act_d7(dir_d7);   dir_m1  = _act_m1(dir_m1)
org_d1  = _act_d1(org_d1);   org_d7  = _act_d7(org_d7);   org_m1  = _act_m1(org_m1)
paid_d1 = _act_d1(paid_d1);  paid_d7 = _act_d7(paid_d7);  paid_m1 = _act_m1(paid_m1)
tot_d1  = _r90_d1(tot_d1);   tot_d7  = _r90_d7(tot_d7);   tot_m1  = _r90_m1(tot_m1)
app_d1  = _r90_d1(app_d1);   app_d7  = _r90_d7(app_d7);   app_m1  = _r90_m1(app_m1)
web_d1  = _r90_d1(web_d1);   web_d7  = _r90_d7(web_d7);   web_m1  = _r90_m1(web_m1)

# Campaign-level data — latest full month only
campaigns = []
try:
    # Fetch top 30 campaigns per month for ALL full months (Jan → last full month)
    last_full = all_months[prev_month_i]
    last_full_end = (last_full.replace(day=28) + datetime.timedelta(days=4)).replace(day=1).strftime('%Y-%m-%d')
    camp_rows = run(f"""
    SELECT * EXCEPT(rn) FROM (
      SELECT *,
        ROW_NUMBER() OVER (PARTITION BY month ORDER BY new_users DESC) as rn
      FROM (
        SELECT
          DATE_TRUNC(visit_date, MONTH) as month,
          campaign,
          LOWER(campaign) as campaign_lc,
          SUM(d0) as new_users,
          SUM(user_20adview_7d) as activated_adview,
          SUM(user_1lead_7d) as activated_lead,
          SAFE_DIVIDE(SUM(user_20adview_7d), SUM(d0)) as activation_rate,
          -- Restricted to full months already, which hides the maturity problem
          -- for most of the month but not all of it: run this on the 3rd and the
          -- D7 frontier sits at the 27th of the previous month, so the last four
          -- days of the "full" month would still dilute every campaign here.
          SAFE_DIVIDE(SUM(IF(visit_date <= DATE '{mat_through('act','d1')}', d1, 0)),
                      SUM(IF(visit_date <= DATE '{mat_through('act','d1')}', d0, 0))) as nurr_d1,
          SAFE_DIVIDE(SUM(IF(visit_date <= DATE '{mat_through('act','d7')}', d7, 0)),
                      SUM(IF(visit_date <= DATE '{mat_through('act','d7')}', d0, 0))) as nurr_d7
        FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
        WHERE return_status = 'new'
          AND campaign NOT IN ('all', '(none)')
          AND channel NOT IN ('all', 'Direct', 'Organic Search', 'web_to_app')
          AND vertical_user = 'all'
          AND LOWER(campaign) NOT LIKE '%web_to_app%'
          AND LOWER(campaign) NOT LIKE '%web2app%'
          AND visit_date >= '2026-01-01' AND visit_date < '{last_full_end}'
        GROUP BY 1, 2, 3
        HAVING SUM(d0) >= 100
      )
    )
    WHERE rn <= 30
    ORDER BY month, new_users DESC
    """)
    def classify_camp(lc):
        if any(k in lc for k in ['pty','property','bds','nha dat','nha_dat','_5010','_5020','_5030','nha_vua','bat_dong_san']): return 'pty'
        if any(k in lc for k in ['job','viec lam','viec_lam','tuyen dung','tuyen_dung']): return 'job'
        if any(k in lc for k in ['veh','vehicle','autox','_2010','_2020','_2030','_2040']): return 'veh'
        if any(k in lc for k in ['gds','elt','electronics','world_cup','awo_rewards','app_install','digital_activate','digital_install']): return 'gds'
        return 'other'
    for r in camp_rows:
        lc = str(r.get('campaign_lc',''))
        m_date = to_date(r['month'])
        campaigns.append({
            'name': str(r['campaign']),
            'vertical': classify_camp(lc),
            'new_users': int(r['new_users']) if r['new_users'] else 0,
            'activated_adview': int(r['activated_adview']) if r['activated_adview'] else 0,
            'activated_lead': int(r['activated_lead']) if r['activated_lead'] else 0,
            'activation_rate': round(float(r['activation_rate']),4) if r['activation_rate'] else None,
            'nurr_d1': round(float(r['nurr_d1']),4) if r['nurr_d1'] else None,
            'nurr_d7': round(float(r['nurr_d7']),4) if r['nurr_d7'] else None,
            'month': m_date.strftime('%b %Y'),
        })
    months_fetched = sorted(set(c['month'] for c in campaigns))
    print(f"  Campaigns: {len(campaigns)} rows OK (months: {months_fetched})")
except Exception as e:
    note_skipped("Campaigns", e)
    campaigns = D.get('campaigns', [])

# ── Detail Camp Performance — Growth team, App phase ────────────────────────
# cost comes from the Google Sheet "[CT] App Growth - performance tracking
# 2026", tab raw_total. Install comes from Airbridge and activation metrics
# from BigQuery. All three are joined on campaign name.
#
# Install used to come from the sheet too — i.e. from each platform's own ads
# manager — and that is what made it disagree with Airbridge. The two count
# different things: a self-attributing network credits itself for a view-through
# it thinks it caused, Airbridge applies one attribution model across every
# channel. The gap is not uniform, which is what made it hard to see: over the
# 27 August campaigns the two totals sit within 0.2% of each other (69,334 vs
# 69,473), most Google campaigns read a few percent LOW in the sheet, and two
# Facebook campaigns read 2x HIGH — ..._2026theme_targeting at 2,244 vs 1,124
# and ..._2026theme at 1,905 vs 820. Netting to zero in aggregate while being
# wrong per campaign is the worst case for a table people read row by row, so
# install is now sourced from Airbridge for every row. Duyen's call, 2026-08-18:
# "tất cả các số install ở đây lấy từ source bảng airbridge chứ k phải từ ads
# manager".
#
# Cost deliberately stays on the sheet: it is real money, it reconciles against
# the target tab, and Airbridge's cost_channel_metric agrees with it to 99-100%
# anyway, so moving it would buy nothing and break that reconciliation.
#
# The sheet is read through the docs.google.com CSV export endpoint rather than
# the Sheets API on purpose: GOOGLE_CREDENTIALS is a user (authorized_user) ADC
# token whose account lacks serviceusage.serviceUsageConsumer on chotot-dwh, so
# any *.googleapis.com call needing a quota project returns 403. docs.google.com
# does not enforce a quota project, so a plain Bearer request works.
SHEET_ID = '1eLdUTKfR9yHcUnnEfyIouZlCiVDPvR6yn3igxdoy8eE'
SHEET_GID = '2065956136'         # raw_total
SHEET_TARGET_GID = '2028964073'  # target

# The target tab's Month column is a bare month number, and the workbook itself
# is per-year ("[CT] App Growth - performance tracking 2026"), so the year has
# to be supplied here. Bump this when a 2027 workbook replaces it.
TARGET_YEAR = 2026

# Growth team / App phase ad accounts -> ad channel
GROWTH_ACCOUNTS = {
    'chotot_growth_sgd': 'FB',
    'chotot_pty_app':    'FB',
    'chotot_job_app':    'FB',
    'chotot_veh_app':    'FB',
    'chotot_app_pty':    'GG',
    'chotot_app_veh':    'GG',
    'chotot_app_job':    'GG',
    'chotot_growth_new': 'GG',
}
SHEET_PHASES = {'install', 'activate'}


def _sheet_token():
    """Access token for the Drive/Docs export endpoint.

    Handles both a service-account key and a user ADC token so the script keeps
    working if GOOGLE_CREDENTIALS is ever swapped for a proper service account.
    """
    from google.auth.transport.requests import Request as GRequest
    path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '/tmp/gcp-creds.json')
    with open(path) as f:
        info = json.load(f)
    if info.get('type') == 'service_account':
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/drive.readonly'])
    else:
        from google.oauth2.credentials import Credentials as UserCredentials
        creds = UserCredentials.from_authorized_user_info(info)
    creds.refresh(GRequest())
    return creds.token


def _sheet_num(v):
    """Sheet numbers carry display thousands separators, e.g. "483,546"."""
    s = str(v or '').replace(',', '').replace('₫', '').strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _sheet_csv(gid):
    """Read one tab of the workbook as a list of CSV rows."""
    import csv, io, urllib.request
    url = (f'https://docs.google.com/spreadsheets/d/{SHEET_ID}'
           f'/export?format=csv&gid={gid}')
    req = urllib.request.Request(
        url, headers={'Authorization': 'Bearer ' + _sheet_token()})
    with urllib.request.urlopen(req, timeout=90) as r:
        text = r.read().decode('utf-8', 'replace')
    return list(csv.reader(io.StringIO(text)))


def fetch_camp_cost():
    """Aggregate cost + install by (month, campaign) for the growth accounts.

    Also returns how far into each month the spend data actually goes. A running
    month is only a few days old, so judging its actual against a whole month's
    target reads as a catastrophic miss when it may well be on pace. The last
    dated row per month is the honest denominator for that, and it has to come
    from here because raw_total is the only day-level source we have.

    Returns (agg, last_day, daily) where last_day maps the month's first-of-month
    date to the latest day seen for it, and daily maps (month, campaign) to
    {day: cost}. The day-level breakdown exists so a cost can be re-summed over a
    shorter window — needed when BigQuery has not published as many days as the
    sheet has, which would otherwise divide a longer stretch of spend by a shorter
    stretch of leads.
    """
    agg, seen, bad_dates = {}, 0, 0
    last_day, daily = {}, {}
    for row in _sheet_csv(SHEET_GID)[1:]:  # [1:] drops the header row
        # Columns N onward hold an unrelated account_name/channel lookup block,
        # so hard-stop at column M.
        row = (row + [''] * 13)[:13]
        date_s, account, camp = row[0].strip(), row[1].strip(), row[2].strip()
        phase, vertical = row[10].strip().lower(), row[11].strip().lower()
        if not date_s or not camp:
            continue
        channel = GROWTH_ACCOUNTS.get(account.lower())
        if channel is None or phase not in SHEET_PHASES:
            continue
        try:
            mth, dy, yr = (int(x) for x in date_s.split('/'))  # M/D/YYYY
            m_date = datetime.date(yr, mth, 1)
            d_date = datetime.date(yr, mth, dy)
        except (ValueError, TypeError):
            bad_dates += 1
            continue
        seen += 1
        prev = last_day.get(m_date)
        if prev is None or d_date > prev:
            last_day[m_date] = d_date
        e = agg.setdefault((m_date, camp), {
            'cost': 0.0, 'install': 0.0, 'channel': channel,
            'vertical': vertical or 'other', 'phases': set(),
        })
        cost = _sheet_num(row[6])
        e['cost'] += cost
        e['install'] += _sheet_num(row[5])
        e['phases'].add(phase)
        d = daily.setdefault((m_date, camp), {})
        d[d_date] = d.get(d_date, 0.0) + cost
    if bad_dates:
        print(f"  Sheet: {bad_dates} rows with unparseable dates skipped")
    print(f"  Sheet: {seen} growth rows -> {len(agg)} campaign-months")
    return agg, last_day, daily


def fetch_targets():
    """Monthly budget / install targets per vertical from the `target` tab.

    Only columns A-H (the Input + Formula target columns) are read. Columns I/J,
    "Actual spend" and "Actual install", are deliberately NOT used as the actual
    values for the progress table, for two reasons:
      1. They are maintained by hand and lag reality — they were still empty for
         July on 2026-07-31 despite 880M ₫ having been spent.
      2. At least one is wrong: May 2026 `job` reads 318,439,532 ₫, which is the
         May job spend (143,582,736) plus the May gds spend (174,856,794) added
         together, i.e. gds is counted twice in that column.
    Actuals are therefore computed from camp_detail, off the same raw_total rows
    the rest of section 6 uses. I/J are still read here purely to warn in the log
    when the sheet disagrees with us, which is how the May bug surfaced.

    Returns (target_rows, sheet_actuals) where sheet_actuals maps
    (month_label, vertical) -> (cost, install) for the non-empty cells only.
    """
    out, sheet_actuals = [], {}
    for row in _sheet_csv(SHEET_TARGET_GID):
        row = (row + [''] * 12)[:12]
        mth_s, vertical = row[0].strip(), row[1].strip().lower()
        if not mth_s.isdigit() or not 1 <= int(mth_s) <= 12 or not vertical:
            continue  # header rows and the trailing blank block
        label = datetime.date(TARGET_YEAR, int(mth_s), 1).strftime('%b %Y')

        a_cost, a_install = row[8].strip(), row[9].strip()
        if a_cost or a_install:
            sheet_actuals[(label, vertical)] = (
                _sheet_num(a_cost), _sheet_num(a_install))

        budget, t_install = _sheet_num(row[2]), _sheet_num(row[3])
        # Future months sit in the tab as placeholder rows of zeros; keeping them
        # would render as "0% of 0 ₫" noise.
        if not budget and not t_install:
            continue
        out.append({
            'month': label,
            'vertical': vertical,
            'budget': round(budget),
            'target_install': round(t_install),
            'bau_budget': round(_sheet_num(row[4])),
            'bau_install': round(_sheet_num(row[5])),
            'test_budget': round(_sheet_num(row[6])),
            'test_install': round(_sheet_num(row[7])),
        })
    print(f"  Targets: {len(out)} month-vertical rows with a target "
          f"({sorted({r['month'] for r in out})})")
    return out, sheet_actuals


camp_detail = []
month_cover = {}
try:
    sheet_agg, sheet_last_day, sheet_daily_cost = fetch_camp_cost()
    if not sheet_agg:
        raise RuntimeError('no growth rows found in raw_total')

    names = sorted({c for _m, c in sheet_agg})
    in_list = ','.join(
        "'" + n.replace('\\', '\\\\').replace("'", "\\'") + "'" for n in names)
    # channel != 'all' because that value is a pre-aggregated total of the real
    # channels; d0 is summed across the remaining channels per campaign name.
    #
    # `lead` counts contact events, not people, which is what was asked for
    # ("tổng số lượt liên hệ"). It is attributed to visit_date, so summing it
    # over the month gives exactly the leads that happened inside the period —
    # and unlike save_ad it covers Facebook, so every campaign here has one.
    # Caveat worth knowing: every row is return_status='new', so this is leads
    # made on the install day. Someone who installs on the 3rd and contacts on
    # the 6th is not in it. The lead_7d/lead_30d columns look like the fix but
    # are per-row averages, not counts — they cannot be summed — and the 30d
    # window is still filling in for recent months, so it would undercount most
    # in exactly the newest month people read first.
    #
    # Upper-bounded by the last day the spend sheet has. raw_total is filled in by
    # hand and is the source that actually lags: on 2026-08-10 the 04:38 run had
    # this table through 08-09 but the sheet only through 08-08, so Aug leads ran a
    # day ahead of Aug cost and blended Cost/Lead read 37,679 ₫ against the matched
    # 41,964 ₫ — 11% too cheap, in the direction that flatters. Capping keeps every
    # figure in this section on the window the Cost column already claims through
    # month_cover.through. The opposite skew, BigQuery trailing the sheet, cannot be
    # fixed here (the cost is already banked) and is handled below by lead_cost.
    sheet_max = max(sheet_last_day.values())
    print(f"  Camp detail: BigQuery capped at {sheet_max}, "
          f"the last day raw_total has spend for")
    #
    # RR D1 and RR D7 divide over matured cohorts only — see the maturity block at
    # the top of this file for why, and note the consequence here: d1 and d0_d1 are
    # counted over a shorter window than d0, so `d1 / d0` as read off the screen
    # will not reproduce RR D1. That is why the mature denominator is published
    # alongside rather than left implicit, and why the front end divides by it.
    # This is the discrepancy that started the whole change. For
    # fb_growth_pty_app_android_install_app_pro_b2s_bau_072126_2026theme over
    # 1/8-10/8 both Looker and this dashboard showed RR D1 24.3% (96/395) — they
    # agreed because they made the same mistake, counting an 08-10 cohort of 83
    # users whose D1 had not happened yet. Over matured cohorts it is 96/312 =
    # 30.8%. RR D7 on the same row went 8/395 = 2.0% to 8/79 = 10.1%.
    # The sibling campaign ..._2026theme_targeting is the sharper case: it started
    # 04/08, so every one of its cohorts is inside the D7 window and the old
    # arithmetic published it as 0.0% D7 retention. It now reads "—".
    act_rows = run(f"""
    SELECT
      DATE_TRUNC(visit_date, MONTH) as month,
      campaign,
      SUM(dau) as dau,
      SUM(d0) as d0,
      SUM(IF(visit_date <= DATE '{mat_through('act','d1')}', d1, 0)) as d1,
      SUM(IF(visit_date <= DATE '{mat_through('act','d1')}', d0, 0)) as d0_d1,
      SUM(IF(visit_date <= DATE '{mat_through('act','d7')}', d7, 0)) as d7,
      SUM(IF(visit_date <= DATE '{mat_through('act','d7')}', d0, 0)) as d0_d7,
      SUM(IF(visit_date <= DATE '{mat_through('act','lead7')}',
             user_1lead_7d, 0)) as lead7,
      SUM(dau_lead) as dau_lead,
      SUM(save_ad) as save_ad,
      MAX(visit_date) as bq_through
    FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
    WHERE return_status = 'new'
      AND vertical_user = 'all'
      AND channel != 'all'
      AND visit_date >= '2026-01-01'
      AND visit_date <= '{sheet_max}'
      AND campaign IN ({in_list})
    GROUP BY 1, 2
    """)
    act = {(to_date(r['month']), str(r['campaign'])): r for r in act_rows}

    # Lead EVENTS (lượt liên hệ), not people, over the 7-day window — the count
    # user_1lead_7d cannot give. Kept as its own query because it lives in a
    # different table: retention_activation_core_event_by_source_channel is at
    # per-user (clientId) grain, where lead_7d is an INTEGER event count that sums
    # cleanly. (In the mapping table above, lead_7d is a FLOAT per-row average that
    # must NOT be summed, and its `lead` is D0-only — this is why the event count
    # has to come from here.) Same maturity cutoff as lead7 so cost/lead-event
    # trims like for like, same sheet_max upper bound, same return_status='new'
    # cohort, summed across channels per campaign name. This table has no
    # channel='all'/campaign='all' aggregate rows, so summing does not double count.
    ev_rows = run(f"""
    SELECT
      DATE_TRUNC(visit_date, MONTH) as month,
      campaign,
      SUM(IF(visit_date <= DATE '{mat_through('act','lead7')}',
             lead_7d, 0)) as lead_event
    FROM ct_digital.retention_activation_core_event_by_source_channel
    WHERE return_status = 'new'
      AND visit_date >= '2026-01-01'
      AND visit_date <= '{sheet_max}'
      AND campaign IN ({in_list})
    GROUP BY 1, 2
    """)
    lead_ev = {(to_date(r['month']), str(r['campaign'])): r for r in ev_rows}

    # REMOVED 2026-08-17: the per-vertical "User LH ngành" / "Cost/User LH ngành"
    # columns, which queried this table at vertical_user grain for dau_lead.
    #
    # They were D0-only, and once Lead moved to a 7-day window a D0 column sitting
    # beside it invites exactly the division that does not work — lead_own / lead
    # across two different windows, which reads as a collapse in own-vertical
    # share that never happened. Duyen's call: if it can only be D0, drop it.
    #
    # It cannot be anything but D0, and this is the part worth not re-deriving.
    # `dau_lead` is the ONLY column in this table that genuinely partitions by
    # vertical. Every 7-day user column — user_1lead_7d, user_1lead_30d — belongs
    # to the same family as `lead`: on a vertical row it carries the user's
    # activity across EVERY vertical, copied into each vertical bucket the user
    # falls into. Measured 2026-01-01..08-09 at the 'all' campaign grain, the
    # `other` bucket is decisive: dau_lead is exactly 0 across 221 days and
    # 392,576 users (structural — 'other' cannot receive a contact), yet the same
    # rows carry user_1lead_7d = 28,711 and user_1lead_30d = 45,194. Those users
    # contacted somewhere else. Summed over the five buckets, dau_lead lands
    # within +1.5% of the 'all' row (369,663 vs 364,264) while user_1lead_7d
    # overshoots by +25.3% (719,768 vs 574,415).
    #
    # The other route is already ruled out: contact events per vertical exist in
    # chotot_data.traffic_lead_detail joined to traffic_visit_detail, but that
    # pair has no Facebook campaigns at all — five FB names checked over 40 days
    # return zero rows while GG ones return ~29k each — and it scans 15.7 GB
    # against this table's 0.04 GB.
    #
    # So: no own-vertical 7-day metric exists today. Restoring the columns needs a
    # new source from DA, not a different query against this table.

    # How far this table has published, per month. Cost/Lead is the first number on
    # the page that divides a sheet figure by a BigQuery one, and the two sources
    # land a day apart in both directions: the cap above covers BigQuery running
    # ahead, this covers it trailing, where the spend is already banked and cannot
    # be capped away. Untested against a real occurrence — as of 2026-08-10 only the
    # other direction has been seen — so if a CPL looks wrong, check here first.
    bq_max = {}
    for r in act_rows:
        m = to_date(r['month'])
        t = to_date(r['bq_through']) if r.get('bq_through') else None
        if t and (m not in bq_max or t > bq_max[m]):
            bq_max[m] = t

    # Last cohort date whose 7-day contact window has closed. Same value the SQL
    # above filtered on, kept as a date so the spend can be trimmed to match.
    lead7_through = MAT.get(('act', 'lead7'), {}).get('through')

    # "Save ad in D0" + its DAU denominator come from the MKT-owned adopt table
    # ct_product_analytics.new_user_adopt_activate (owner ngan_vuthien).
    #
    # That table was rebuilt on 2026-08-21 and the rebuild broke this block for
    # two nightly runs (22/8 and 23/8) — the whole camp-detail section fell into
    # its except handler and republished the previous day's rows, which is why
    # section 6 and the "data đến" badge sat at 08-19 while every other section
    # kept moving. Three things changed, all of them load-bearing here:
    #
    #  1. adopt_users -> save_ad_d0_users. This is the one that raised
    #     "Unrecognized name: adopt_users". The rebuild also split save_ad and
    #     make_lead into d0 / d0_d3 / d0_d7 columns; only the d0 one belongs in a
    #     column labelled "Save ad in D0".
    #  2. report_date used to be first_date + 7, so the cohort month was
    #     report_date - 7 and rows only appeared once the 7-day window had
    #     matured. It is now first_date itself and rows appear the next day —
    #     verified 2026-08-24, max(report_date) = 08-23. Keeping the DATE_SUB
    #     would have silently shifted every cohort a week early instead of
    #     erroring, so this is the more dangerous half of the change.
    #  3. vertical / category / login_status / platform are new dimensions, and
    #     the table's own description warns it does not dedupe: vertical and
    #     category are the union of save_ad + make_lead + view_ad over d0-d7, so
    #     one user lands in every vertical they touched. Without the 'all'
    #     filters DAU double-counts (2026-08-10 iOS/login: 10,970 at vertical
    #     'all' vs 4,772 gds + 4,106 pty + ... summing well past it). There is no
    #     'all' row for login_status or platform, so those two are summed.
    #
    # Regression-checked against the last good run (Jul 2026, per campaign):
    # pty_appinstall_mass_android 25,080 -> 25,136 DAU / 1,358 -> 1,353 save;
    # job_appinstall_inapp_android_adview_7d 23,064 -> 23,903 / 1,986 -> 2,034;
    # gds_appinstall_inapp_android_adview_7d 5,371 -> 4,195 / 436 -> 342. The
    # absolute counts moved a few percent to 20% because the rebuild reattributed
    # them upstream, but save_ad_rate — the number actually on the page — held to
    # within 0.4pp on every row, which is what a dimension mistake would not do.
    adopt_rows = run(f"""
    SELECT
      DATE_TRUNC(report_date, MONTH) as month,
      campaign,
      SUM(dau) as dau,
      SUM(save_ad_d0_users) as save_ad_d0,
      MAX(report_date) as max_cohort
    FROM ct_product_analytics.new_user_adopt_activate
    WHERE channel != 'all'
      AND vertical = 'all'
      AND category = 'all'
      AND report_date >= '2026-01-01'
      AND campaign IN ({in_list})
    GROUP BY 1, 2
    """)
    adopt = {(to_date(r['month']), str(r['campaign'])): r for r in adopt_rows}

    # Last cohort date the adopt table has matured, per month — used to mark the
    # month whose save-ad counts are still filling in.
    adopt_max = {}
    for r in adopt_rows:
        m = to_date(r['month'])
        c = to_date(r['max_cohort']) if r['max_cohort'] else None
        if c and (m not in adopt_max or c > adopt_max[m]):
            adopt_max[m] = c

    # Install, per (month, campaign), from Airbridge. See the note at the top of
    # this section for why it no longer comes from the sheet.
    #
    # Three things about this table are worth not re-deriving:
    #
    #  1. Each row is either a cost row (device_type='Unknown', carries cost and
    #     impressions, installs = 0) or an attribution row (device_type mobile /
    #     other / tablet, carries the installs, cost = 0). They are disjoint, so
    #     summing across all of them double-counts nothing — but it also means a
    #     single row can never yield a CPI on its own.
    #  2. No channel filter. For the campaign names this dashboard tracks, the
    #     paid channels (google.adwords, facebook.business) hold 647,641 installs
    #     over Jan–Aug and every other channel put together holds 66 — 0.01%.
    #     Filtering would buy nothing and would silently drop a campaign if
    #     Airbridge ever relabels a channel.
    #  3. GREATEST of the two install metrics, not either one alone.
    #     app_installs_metric counts events and app_install_users_metric counts
    #     people, so events >= users must hold, and in settled months it does —
    #     at this day grain, `users > events` happens on 0 of the 5,199
    #     campaign-days in Jan through Jul and on 4 of the 472 in August. It
    #     breaks only in the month still filling in, where each metric has its
    #     own holes: ..._b2s_bau_080626_targeting has steady events (164, 128,
    #     164, 180, 200) against erratic users (0, 25, 17, 78, 200), while
    #     gg_growth_veh_... has events 107 then five days of 0 against steady
    #     users (106, 164, 104, 147, 101). Taking the greater picks whichever
    #     side has actually loaded, and self-heals as the backfill arrives: it
    #     adds 516 installs to August and exactly 0 to every closed month.
    #
    # Summed **only over the days the sheet has a cost row for that campaign** —
    # Duyen's rule, 2026-08-18: "cost lấy của campaign nào thì install lấy của
    # campaign đó mapping theo là được". A whole-month cap is not enough, because
    # the sheet's window differs per campaign, not just per month: in March the
    # pty rows disagreed with Airbridge on install by 2.4-3.0x AND on cost by
    # 1.7-2.5x in the same direction, while every job_* row matched both to
    # within 1%. Same-direction cost drift is not an attribution difference, it
    # is a shorter window — so the fix is to give install exactly the window the
    # cost has, campaign by campaign. Anything else divides one span of spend by
    # a different span of installs and calls it CPI.
    ab_rows = run(f"""
    SELECT
      campaign,
      event_date,
      GREATEST(SUM(app_installs_metric),
               SUM(app_install_users_metric)) as install
    FROM chotot_airbridge.airbridge_attributed_impression_raw
    WHERE event_date BETWEEN DATE '{min(sheet_last_day).replace(day=1)}'
                         AND DATE '{max(sheet_last_day.values())}'
      AND campaign IN ({in_list})
    GROUP BY 1, 2
    """)
    # (campaign, day) -> install, then folded up per (month, campaign) against
    # the sheet's own day list. A day Airbridge has but the sheet does not is
    # dropped; a day the sheet has but Airbridge does not contributes 0.
    ab_daily = {}
    for r in ab_rows:
        v = r['install']
        if v is None:
            continue
        ab_daily[(str(r['campaign']), to_date(r['event_date']))] = int(round(v))
    ab_install, ab_days_used, ab_days_dropped = {}, 0, 0
    # NOTE: every name bound here is a MODULE-level global (this block only looks
    # nested because of the enclosing try:). Prefix loop variables with ab_ —
    # plain `days` and `total` would clobber the month-length list and the
    # channel totals that the sections below still read. Both have already
    # broken a CI run once.
    for (m_date, camp), ab_cd in sheet_daily_cost.items():
        ab_total, hit = 0, False
        for d in ab_cd:
            v = ab_daily.get((camp, d))
            if v is not None:
                ab_total += v
                hit = True
        if hit:
            ab_install[(m_date, camp)] = ab_total
        ab_days_used += len(ab_cd)
    ab_days_dropped = sum(
        1 for (camp, d) in ab_daily
        if d not in sheet_daily_cost.get((d.replace(day=1), camp), {}))

    # Campaign status (running / paused), asked for 2026-08-27 to replace the Ch
    # column: "để biết campaign đó đang active hay off".
    #
    # There is no status field to read. Not in the sheet (columns stop at M and
    # none of them is a status), not in Airbridge, not in the adopt or activation
    # tables. The real one lives in Ads Manager, and CI has no ads-platform
    # credentials — only BigQuery and plain HTTP. So status here is *inferred
    # from spend*, on Duyen's rule: "xem ngày gần nhất, vd hôm nay lúc kéo data
    # ngày hôm qua 26. nếu thấy k có spend thì update status là pause".
    #
    # Two things make that inference safe rather than a guess:
    #  1. Airbridge cost is a day fresher than the sheet — it had 2026-08-26 on
    #     the morning of 08-27, while raw_total was still on 08-24. Judging
    #     "still running" off the sheet would mark three days of live campaigns
    #     as paused.
    #  2. The feed's last day is not half-loaded, which would make every
    #     campaign look paused. Measured 08-20..08-26: of the campaigns spending
    #     on a given day, 0-2 (0.9-1.9%) have no row the next day, and the last
    #     day carries the *highest* campaign count of the week (107). Day-to-day
    #     dropout that small is real pausing, not ingestion lag.
    #
    # The reference day is the whole feed's max, not the max over our campaigns:
    # if every growth campaign paused at once, the latter would silently redefine
    # "today" as the last day we spent and report everything as running.
    #
    # Status is per campaign and current, not per (month, campaign) — Duyen chose
    # "luôn hiện status hiện tại" so a Mar row still answers "is this one alive
    # now". Hence a separate map keyed by name rather than a field on each row.
    status_rows = run(f"""
    SELECT
      campaign,
      MAX(event_date) as last_cost_day,
      (SELECT MAX(event_date)
       FROM chotot_airbridge.airbridge_attributed_impression_raw
       WHERE cost_channel_metric > 0
         AND event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)) as feed_last_day
    FROM chotot_airbridge.airbridge_attributed_impression_raw
    WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 180 DAY)
      AND cost_channel_metric > 0
      AND campaign IN ({in_list})
    GROUP BY 1
    """)
    status_asof = max((to_date(r['feed_last_day']) for r in status_rows
                       if r['feed_last_day']), default=None)
    camp_status = {}
    for r in status_rows:
        last = to_date(r['last_cost_day'])
        camp_status[str(r['campaign'])] = {
            'active': bool(status_asof and last >= status_asof),
            'last_cost_day': last.strftime('%Y-%m-%d'),
        }

    def _int(v):
        return int(v) if v is not None else None

    def _rate(num, den):
        """Unlike safe_div, a real zero numerator stays 0.0 instead of becoming
        None — a campaign with genuinely 0 D7 retention must not render as "—".
        """
        if num is None or not den:
            return None
        return round(num / den, 4)

    def _month_end(m_date):
        """Last day of the month m_date starts, capped at today so the current
        month isn't reported as partial merely because it hasn't finished."""
        nxt = datetime.date(m_date.year + (m_date.month == 12),
                            m_date.month % 12 + 1, 1)
        return min(nxt - datetime.timedelta(days=1), today)

    # Sheet-vs-Airbridge install, per month, printed below. Kept because the two
    # sources drift apart per campaign while agreeing in total, so a month-level
    # figure is the only cheap way to notice the day one of them breaks.
    ab_recon = {}
    ab_missing = []

    for (m_date, name), s in sorted(sheet_agg.items()):
        a = act.get((m_date, name), {})
        d0 = _int(a.get('d0'))
        d1, d7 = _int(a.get('d1')), _int(a.get('d7'))
        # Denominators for RR D1 / RR D7: the same d0, restricted to cohorts old
        # enough to have had a D1 (resp. D7). Equal to d0 for every finished
        # month; smaller only where the month is still running.
        d0_d1, d0_d7 = _int(a.get('d0_d1')), _int(a.get('d0_d7'))
        lead = _int(a.get('lead7'))
        # DAU and save-ad-in-D0 both come from the adopt table so the % is an
        # internally consistent ratio. Mixing in the retention table's DAU here
        # would divide an app-only numerator by an all-platform denominator.
        ad = adopt.get((m_date, name), {})
        dau = _int(ad.get('dau'))
        save_ad_d0 = _int(ad.get('save_ad_d0'))
        last_cohort = adopt_max.get(m_date)
        # Install is Airbridge's, not the sheet's. A campaign the sheet has
        # spend for but Airbridge has no row for renders "—" rather than
        # falling back to the ads-manager figure: a silent fallback would put a
        # number from the rejected source into a column labelled as coming from
        # Airbridge, which is the one outcome this change exists to prevent.
        # The sheet still anchors the row, so its cost is published either way.
        cost = round(s['cost'])
        install = ab_install.get((m_date, name))
        sheet_install = int(s['install'])
        e = ab_recon.setdefault(m_date, [0, 0, 0, 0])
        e[0] += sheet_install
        e[1] += install or 0
        e[2] += 1
        if install is None:
            e[3] += 1
            ab_missing.append((m_date, name, sheet_install, cost))
        # Cost restricted to the days BigQuery has actually published, so CPL
        # divides like for like. `cost` itself stays whole: it is real money and
        # the progress-vs-target table reconciles it against the sheet, so
        # trimming it there would make the dashboard contradict its own source.
        #
        # Two things can end the lead window early and the tighter one wins: how
        # far the table has published (bq_max) and how far its cohorts have
        # matured (lead7_through = max_date - 7). On a running month the second
        # is always the binding one, and it is the one that used to be missing:
        # a cohort from three days ago has had three of its seven days, so it
        # arrives partly counted and drags the month down without ever looking
        # broken. Spend is trimmed to the same day so Cost/Lead stays like for
        # like; a finished month is untouched, both cutoffs sit past its end.
        cutoff = bq_max.get(m_date)
        if lead7_through is not None:
            cutoff = min(cutoff, lead7_through) if cutoff else lead7_through
        if cutoff is not None and cutoff < sheet_last_day.get(m_date, cutoff):
            lead_cost = round(sum(
                c for d, c in sheet_daily_cost.get((m_date, name), {}).items()
                if d <= cutoff))
        else:
            lead_cost = cost
        # The month's lead figure covers cohorts up to this day. Self-clearing:
        # it is recomputed from the source every run, so a month flagged today
        # loses its star of its own accord once max_date has moved seven days
        # past the month end — no manual reset, no stale asterisk.
        lead_through = min(cutoff, _month_end(m_date)) if cutoff else None
        lead_partial = bool(
            lead_through and lead_through < sheet_last_day.get(m_date, lead_through))
        # Lead EVENTS in the same 7-day window (lượt, not người), from the
        # per-user core-event table. Trimmed by the same cutoff in SQL, so it
        # shares this row's lead_cost / lead_through / lead_partial — cpl_event
        # divides over exactly the same days as cpl does.
        le = lead_ev.get((m_date, name), {})
        lead_event = _int(le.get('lead_event'))
        camp_detail.append({
            'name': name,
            'month': m_date.strftime('%b %Y'),
            'channel': s['channel'],
            'vertical': s['vertical'],
            'phase': '+'.join(sorted(s['phases'])),
            'cost': cost,
            'install': install,
            'cpi': round(cost / install) if install else None,
            'd0': d0,
            'd1': d1,
            'd7': d7,
            'd0_d1': d0_d1,
            'd0_d7': d0_d7,
            'rr_d1': _rate(d1, d0_d1),
            'rr_d7': _rate(d7, d0_d7),
            # New users who contacted a seller within 7 days of installing —
            # people, not contact events, and counted over a 7-day window rather
            # than D0 only. Sections 3 and 4 have always used this column; only
            # this section still summed `lead`, so the two never reconciled.
            'lead': lead,
            # A campaign with cost but zero leads has no CPL to quote — None
            # renders as "—" rather than as a division by zero.
            'cpl': round(lead_cost / lead) if lead else None,
            # Lead EVENTS (lượt) in 7 days and cost per event. Same lead_cost as
            # cpl so both cost-per numbers cover the same window; 0/None → "—".
            'lead_event': lead_event,
            'cpl_event': round(lead_cost / lead_event) if lead_event else None,
            # Set while the newest cohorts are still inside their 7 days, so the
            # count is real but not yet final. Clears itself once they mature.
            **({'lead_partial': True} if lead_partial else {}),
            **({'lead_through': lead_through.strftime('%d/%m')}
               if lead_partial else {}),
            # Only emitted when it differs from cost, i.e. when BigQuery is
            # behind the sheet; the front end uses it to blend CPL over the
            # same window and to say so.
            **({'lead_cost': lead_cost} if lead_cost != cost else {}),
            'dau': dau,
            'save_ad_d0': save_ad_d0,
            'save_ad_rate': _rate(save_ad_d0, dau),
            # True when the month's cohorts have not all matured yet, so the
            # absolute save-ad/DAU counts are still incomplete. Two ways that
            # happens now: the table has not published to the end of the window
            # yet (first clause), or it has but the newest cohorts are still
            # inside the 7 days the table's own description says its rolling
            # metrics keep self-updating for (second clause). Before the
            # 2026-08-21 rebuild only the first could happen, because rows did
            # not appear at all until they had matured; now they appear the next
            # day, so a month can look complete and still move. Both clauses
            # clear themselves with time.
            'save_partial': bool(
                last_cohort and (last_cohort < _month_end(m_date)
                                 or last_cohort > today - datetime.timedelta(days=7))),
            'save_through': last_cohort.strftime('%d/%m') if last_cohort else None,
        })
    matched = sum(1 for r in camp_detail if r['d0'] is not None)
    save_matched = sum(1 for r in camp_detail if r['save_ad_d0'] is not None)
    # cd_ prefix on purpose: this module already has a module-level `lead_total`
    # (the section-3 activation series, a per-month list) and reusing that name
    # here silently replaced the list with an int, which only blew up 200 lines
    # later where lead_rate indexes into it.
    cd_lead_matched = sum(1 for r in camp_detail if r['lead'] is not None)
    cd_lead_total = sum(r['lead'] or 0 for r in camp_detail)
    by_ch = {}
    for r in camp_detail:
        k = r['channel']
        by_ch.setdefault(k, [0, 0])
        by_ch[k][0] += 1
        if r['save_ad_d0'] is not None:
            by_ch[k][1] += 1
    det_months = sorted({r['month'] for r in camp_detail})

    # How much of each month the spend actually covers. Used by the front end to
    # judge a running month against the pace it should be at, not against a whole
    # month it has not had the days to reach yet.
    for m_date, last in sorted(sheet_last_day.items()):
        # calendar.monthrange, not _month_end() — the latter caps at today, which
        # would make the running month look like a full one (10 of 10 days).
        dim = calendar.monthrange(m_date.year, m_date.month)[1]
        bq_t = bq_max.get(m_date)
        month_cover[m_date.strftime('%b %Y')] = {
            'through': last.strftime('%Y-%m-%d'),
            'days': last.day,
            'days_in_month': dim,
            'elapsed': round(last.day / dim, 4),
            # Only set when BigQuery trails the sheet, so the front end can say
            # which window the lead numbers really cover.
            **({'bq_through': bq_t.strftime('%Y-%m-%d')}
               if bq_t and bq_t < last else {}),
        }
    running = [f"{k} through {v['through']} ({v['days']}/{v['days_in_month']}d"
               f" = {v['elapsed']:.0%})"
               for k, v in month_cover.items() if v['days'] < v['days_in_month']]
    if running:
        print(f"  Month coverage (incomplete): {'; '.join(running)}")

    print(f"  Camp detail: {len(camp_detail)} rows OK "
          f"({matched} matched BQ activation, months: {det_months})")
    ab_rows_ok = sum(e[2] - e[3] for e in ab_recon.values())
    print(f"  Install source: Airbridge, {ab_rows_ok}/{len(camp_detail)} rows "
          f"matched (the rest render \"—\" rather than fall back to the sheet); "
          f"summed over the {ab_days_used:,} campaign-days the sheet has cost "
          f"for, {ab_days_dropped:,} Airbridge campaign-days outside that window "
          f"dropped")
    # NOT `n` — that is the module-level month count this file indexes every
    # published array by, and rebinding it here left it at 27 and blew up
    # ret_d1_gc a thousand lines later with an IndexError.
    for m_date, (s_i, a_i, n_rows, miss) in sorted(ab_recon.items()):
        ab_delta = f"{(a_i - s_i) / s_i:+.1%}" if s_i else "n/a"
        flag = ''
        # A month where the two sources disagree by more than a tenth is worth a
        # human look — not necessarily wrong, but it is no longer the quiet
        # methodology gap the switch was made for.
        if s_i and abs(a_i - s_i) / s_i > 0.10:
            flag = '  <-- check'
        print(f"    {m_date:%b %Y}: sheet {s_i:,} -> Airbridge {a_i:,} "
              f"({ab_delta}, {n_rows - miss}/{n_rows} rows){flag}")
    for ab_m, ab_name, s_i, c in ab_missing[:10]:
        print(f"    no Airbridge row: {ab_m:%b %Y} {ab_name} "
              f"(sheet said {s_i:,} installs on {c:,.0f} ₫)")
    if len(ab_missing) > 10:
        print(f"    ... and {len(ab_missing) - 10} more with no Airbridge row")
    cd_lead_partial = sum(1 for r in camp_detail if r.get('lead_partial'))
    print(f"  Lead: {cd_lead_matched}/{len(camp_detail)} rows have a lead count, "
          f"{cd_lead_total:,} users contacting within 7 days "
          f"(unlike save_ad this covers FB, so a low match rate here is a bug); "
          f"cohorts matured through {lead7_through}, "
          f"{cd_lead_partial} rows still filling in")
    for m_date, last in sorted(sheet_last_day.items()):
        bq_t = bq_max.get(m_date)
        if bq_t and bq_t < last:
            trimmed = sum(r.get('lead_cost', r['cost']) for r in camp_detail
                          if r['month'] == m_date.strftime('%b %Y'))
            full = sum(r['cost'] for r in camp_detail
                       if r['month'] == m_date.strftime('%b %Y'))
            print(f"  CPL window: {m_date:%b %Y} spend runs to {last} but "
                  f"BigQuery only to {bq_t}; CPL divides {trimmed:,} ₫ "
                  f"instead of {full:,} ₫ so it is not inflated by "
                  f"{full - trimmed:,} ₫ of spend with no leads loaded yet")
    print(f"  Save ad in D0: {save_matched}/{len(camp_detail)} rows matched "
          f"new_user_adopt_activate "
          + ' '.join(f'{k}={v[1]}/{v[0]}' for k, v in sorted(by_ch.items()))
          # Was "FB coverage is expected to be low until DA backfills it" — the
          # 2026-08-21 rebuild of the table added Facebook and the first run
          # after it came back FB=141/144, so a low FB number here is now a
          # regression to look into rather than the known state of the world.
          + " (both channels should be near-complete since 2026-08-21)")
    cs_on = sum(1 for v in camp_status.values() if v['active'])
    cs_named = {r['name'] for r in camp_detail}
    print(f"  Campaign status: as of {status_asof} (Airbridge's last day with "
          f"spend), {cs_on} of {len(camp_status)} campaigns still spending, "
          f"{len(cs_named - set(camp_status))} on the table with no Airbridge "
          f"cost row at all (render \"—\")")
except Exception as e:
    note_skipped("Camp detail", e)
    camp_detail = D.get('camp_detail', [])
    month_cover = D.get('month_cover', {})
    camp_status = D.get('camp_status', {})
    status_asof = D.get('camp_status_asof')

# Monthly targets per vertical, for the progress table in section 6.
camp_target = D.get('camp_target', [])
try:
    camp_target, sheet_actuals = fetch_targets()

    # Tripwire: our actuals and the sheet's hand-typed ones should agree, since
    # both ultimately describe the same spend. Warn instead of failing — a stale
    # or half-filled Actual column is normal mid-month and is not our problem.
    ours = {}
    for r in camp_detail:
        k = (r['month'], r['vertical'])
        e = ours.setdefault(k, [0.0, 0.0])
        e[0] += r['cost']
        # install is None on a row Airbridge has no match for — see the camp
        # detail section. Treated as 0 here, which is what the progress table
        # does too, so the tripwire compares the same total the page shows.
        e[1] += r['install'] or 0
    for k, (s_cost, s_inst) in sorted(sheet_actuals.items()):
        o_cost, o_inst = ours.get(k, (0.0, 0.0))
        if s_cost and o_cost and abs(s_cost - o_cost) / s_cost > 0.10:
            print(f"  NOTE target tab Actual spend disagrees for {k[0]} {k[1]}: "
                  f"sheet {s_cost:,.0f} vs raw_total {o_cost:,.0f} "
                  f"({(o_cost - s_cost) / s_cost:+.1%}) — the dashboard uses "
                  f"raw_total")
except Exception as e:
    note_skipped("Targets", e)

# Vertical monthly breakdown — full 2026 trend
def classify_vertical(lc):
    if any(k in lc for k in ['pty','property','bds','nha dat','nha_dat','_5010','_5020','_5030','nha_vua','bat_dong_san']): return 'pty'
    if any(k in lc for k in ['job','viec lam','viec_lam','tuyen dung','tuyen_dung']): return 'job'
    if any(k in lc for k in ['veh','vehicle','autox','_2010','_2020','_2030','_2040']): return 'veh'
    if any(k in lc for k in ['gds','elt','electronics']): return 'gds'
    return 'other'

vertical_monthly = D.get('vertical_monthly', {})
try:
    vm_rows = run("""
    SELECT
      DATE_TRUNC(visit_date, MONTH) as month,
      LOWER(campaign) as campaign_lc,
      SUM(d0) as new_users,
      SUM(user_20adview_7d) as activated_adview
    FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
    WHERE return_status = 'new'
      AND campaign NOT IN ('all', '(none)')
      AND channel NOT IN ('all', 'Direct', 'Organic Search', 'web_to_app')
      AND LOWER(campaign) NOT LIKE '%web_to_app%'
      AND LOWER(campaign) NOT LIKE '%web2app%'
      AND vertical_user = 'all'
      AND visit_date >= '2026-01-01'
    GROUP BY 1, 2
    ORDER BY 1, 2
    """)
    # Aggregate by (month, classified_vertical) using campaign name keywords
    vm_lookup = {}
    for r in vm_rows:
        m = to_date(r['month'])
        vert = classify_vertical(str(r.get('campaign_lc', '')))
        if vert == 'other':
            continue
        key = (m, vert)
        if key not in vm_lookup:
            vm_lookup[key] = {'new_users': 0, 'activated_adview': 0}
        vm_lookup[key]['new_users'] += int(r['new_users'] or 0)
        vm_lookup[key]['activated_adview'] += int(r['activated_adview'] or 0)
    def vm_arr(vert, key):
        return [vm_lookup.get((m, vert), {}).get(key, 0) for m in all_months]
    vertical_monthly = {
        'pty_new_users': vm_arr('pty', 'new_users'),
        'job_new_users': vm_arr('job', 'new_users'),
        'veh_new_users': vm_arr('veh', 'new_users'),
        'gds_new_users': vm_arr('gds', 'new_users'),
        'pty_activated': vm_arr('pty', 'activated_adview'),
        'job_activated': vm_arr('job', 'activated_adview'),
        'veh_activated': vm_arr('veh', 'activated_adview'),
        'gds_activated': vm_arr('gds', 'activated_adview'),
    }
    print(f"  Vertical monthly: OK ({len(vm_rows)} rows)")
except Exception as e:
    note_skipped("Vertical monthly", e)

# Attribution assist — % of Direct/Organic users that are Growth-campaign last-touch attributed
attribution_assist = D.get('attribution_assist', {'direct_pct': [None]*n, 'organic_pct': [None]*n})
try:
    aa_rows = run("""
    SELECT
      DATE_TRUNC(visit_date, MONTH) as month,
      channel,
      SUM(CASE WHEN campaign = 'all' THEN d0 ELSE 0 END) as total_channel,
      SUM(CASE WHEN campaign NOT IN ('all','(none)')
           AND NOT REGEXP_CONTAINS(LOWER(campaign), r'web.to.app|web2app')
           THEN d0 ELSE 0 END) as growth_campaign_attributed
    FROM ct_digital.dashboard__retention_mapping_activation_by_source_campaign
    WHERE return_status = 'new'
      AND vertical_user = 'all'
      AND channel IN ('Direct', 'Organic Search')
      AND visit_date >= '2026-01-01'
    GROUP BY 1, 2
    ORDER BY 1, 2
    """)
    aa_lookup = {}
    for r in aa_rows:
        aa_lookup[(to_date(r['month']), r['channel'])] = r
    def aa_pct(ch):
        out_arr = []
        for m in all_months:
            row = aa_lookup.get((m, ch), {})
            total = int(row.get('total_channel') or 0)
            tagged = int(row.get('growth_campaign_attributed') or 0)
            out_arr.append(round(tagged/total, 4) if total else None)
        return out_arr
    attribution_assist = {
        'direct_pct': aa_pct('Direct'),
        'organic_pct': aa_pct('Organic Search'),
    }
    print(f"  Attribution assist: {len(aa_rows)} rows OK")
except Exception as e:
    note_skipped("Attribution assist", e)

# Cost — managed manually via Budget tab in dashboard (localStorage)
# Keep existing cost data from data.json; do not overwrite with BQ or Sheet data.
cost = pad(D['growth_channel']['cost'], n)
new_forecast = D['growth_channel'].get('cost_forecast', [])
gc_new = growth_n
ret_d1_gc=[round(gc_new[i]*(paid_d1[i] or 0)) if gc_new[i] else None for i in range(n)]
ret_d7_gc=[round(gc_new[i]*(paid_d7[i] or 0)) if gc_new[i] else None for i in range(n)]
ret_m1_gc=[round(gc_new[i]*(paid_m1[i] or 0)) if gc_new[i] and paid_m1[i] else None for i in range(n)]

# Build output
out = {
    "updated_at": today.strftime("%Y-%m-%d"),
    "months": months_labels,
    "partial_months": partial,
    "overview": {
        "mau_app": mau_app, "mau_login": mau_login,
        "mau_nonlogin": [a-b if a and b else None for a,b in zip(mau_app,mau_login)],
        "avg_dau": avg_dau, "total_ct_mau": ct_mau,
        "web_other_mau": [a-b if a and b else None for a,b in zip(ct_mau,mau_app)],
        "new_mau": total_n, "new_login_mau": new_login_total,
        "avg_new_dau": daily(total_n, days),
        "returning_mau": [a-b if a and b else None for a,b in zip(mau_app,total_n)],
        "pct_new": [safe_div(total_n[i],mau_app[i]) for i in range(n)],
        "login_rate": [safe_div(mau_login[i],mau_app[i]) for i in range(n)],
        "new_login_rate": [safe_div(new_login_total[i],total_n[i]) for i in range(n)],
        "pct_app_ct": [safe_div(mau_app[i],ct_mau[i]) for i in range(n)],
    },
    "acquisition": {
        "direct": direct_n, "organic": organic_n, "growth": gc_new, "other": other_n,
        "direct_daily": daily(direct_n,days), "organic_daily": daily(organic_n,days),
        "growth_daily": daily(gc_new,days), "other_daily": daily(other_n,days),
        "growth_pct_total": [safe_div(gc_new[i],total_n[i]) for i in range(n)],
    },
    "activation": {
        "adview_total": adview_total, "lead_total": lead_total, "save_total": save_total,
        "adview_rate": [safe_div(adview_total[i],total_n[i]) for i in range(n)],
        "lead_rate": [safe_div(lead_total[i],total_n[i]) for i in range(n)],
        "save_rate": [safe_div(save_total[i],total_n[i]) if save_total[i] else None for i in range(n)],
        "adview_daily": daily(adview_total,days), "lead_daily": daily(lead_total,days),
        "save_daily": daily(save_total,days),
        "direct_adview": dir_adv, "organic_adview": org_adv, "growth_adview": growth_adv,
        "direct_lead": dir_lead, "organic_lead": org_lead, "growth_lead": growth_lead,
        "direct_save": dir_save, "organic_save": org_save, "growth_save": growth_save,
        "direct_adview_daily": daily(dir_adv,days), "organic_adview_daily": daily(org_adv,days),
        "growth_adview_daily": daily(growth_adv,days),
        "direct_lead_daily": daily(dir_lead,days), "organic_lead_daily": daily(org_lead,days),
        "growth_lead_daily": daily(growth_lead,days),
        "direct_save_daily": daily(dir_save,days), "organic_save_daily": daily(org_save,days),
        "growth_save_daily": daily(growth_save,days),
    },
    "retention": {
        "total_d1": tot_d1, "total_d7": tot_d7, "total_m1": tot_m1,
        "app_d1": app_d1, "app_d7": app_d7, "app_m1": app_m1,
        "web_d1": web_d1, "web_d7": web_d7, "web_m1": web_m1,
        "nurr_d1": nurr_d1, "nurr_d7": nurr_d7, "nurr_m1": nurr_m1,
        "direct_d1": dir_d1, "direct_d7": dir_d7, "direct_m1": dir_m1,
        "organic_d1": org_d1, "organic_d7": org_d7, "organic_m1": org_m1,
        "growth_d1": paid_d1, "growth_d7": paid_d7, "growth_m1": paid_m1,
    },
    "growth_channel": {
        "new_users": gc_new, "avg_new_dau": daily(gc_new,days),
        "adview_activated": growth_adv, "lead_activated": growth_lead,
        "adview_rate": [safe_div(growth_adv[i],gc_new[i]) for i in range(n)],
        "lead_rate": [safe_div(growth_lead[i],gc_new[i]) for i in range(n)],
        "adview_daily": daily(growth_adv,days), "lead_daily": daily(growth_lead,days),
        "nurr_d1": paid_d1, "nurr_d7": paid_d7, "nurr_m1": paid_m1,
        "cost": cost, "cost_forecast": new_forecast,
        "pct_of_total_new": [safe_div(gc_new[i],total_n[i]) for i in range(n)],
        "retained_d1": ret_d1_gc, "retained_d7": ret_d7_gc, "retained_m1": ret_m1_gc,
        "cpa": [round(cost[i]/gc_new[i]) if cost[i] and gc_new[i] else None for i in range(n)],
        "caa": [round(cost[i]/growth_adv[i]) if cost[i] and growth_adv[i] else None for i in range(n)],
        "crr_d1": [round(cost[i]/ret_d1_gc[i]) if cost[i] and ret_d1_gc[i] else None for i in range(n)],
        "crr_d7": [round(cost[i]/ret_d7_gc[i]) if cost[i] and ret_d7_gc[i] else None for i in range(n)],
        "crr_m1": [round(cost[i]/ret_m1_gc[i]) if cost[i] and ret_m1_gc[i] else None for i in range(n)],
    },
    "vertical_monthly": vertical_monthly,
    "attribution_assist": attribution_assist,
    "campaigns": campaigns,
    "camp_detail": camp_detail,
    "camp_target": camp_target,
    # Keyed by campaign name, not by (month, campaign): this is the campaign's
    # state right now, deliberately the same on a Mar row as on an Aug one. See
    # the block that builds it for why it is inferred from spend rather than read
    # from a status field.
    "camp_status": camp_status,
    "camp_status_asof": status_asof,
    "month_cover": month_cover,
    # The cohort window behind every retention rate on the page, so the front end
    # can name the date instead of leaving a reader to guess why the dashboard and
    # Looker Studio disagree. `stale` marks a column that stopped publishing early
    # rather than one whose newest cohorts are simply too young; `gaps` are days
    # missing inside the window, which understate the rate rather than biasing it
    # any particular way.
    "maturity": {
        f"{src}.{metric}": {
            "through": m['through'].strftime('%Y-%m-%d') if m['through'] else None,
            "stale": m['stale'],
            "gaps": [g.strftime('%Y-%m-%d') for g in m['gaps']],
        }
        for (src, metric), m in sorted(MAT.items())
    },
    "daily_activation": [
        {
            "date": str(r["visit_date"]),
            "channel": str(r["channel"]),
            "new_users": int(r["new_users"]) if r["new_users"] else 0,
            "adview_activated": int(r["adview_activated"]) if r["adview_activated"] else 0,
            "lead_activated": int(r["lead_activated"]) if r["lead_activated"] else 0,
            "save_activated": int(r["save_activated"]) if r["save_activated"] else 0,
        }
        for r in daily_act_rows
    ] if daily_act_rows else D.get("daily_activation", []),
}

with open(DATA_JSON, 'w') as f:
    json.dump(out, f, indent=2, default=str)

print(f"✅ data.json updated — {months_labels}")
print(f"   Latest: {months_labels[-1]} | App MAU: {mau_app[-1]:,} | New: {total_n[-1]:,}")

# Written for the workflow's last step, which fails the run when this file
# exists. See the note next to note_skipped() for why the failure is raised
# there and not here. Deleted when nothing was skipped, so a leftover file from
# an earlier run on the same runner cannot fail a good one.
if SKIPPED:
    with open(SKIPPED_FILE, 'w') as f:
        f.write('\n'.join(SKIPPED) + '\n')
    print(f"⚠️  {len(SKIPPED)} section(s) republished stale data: "
          f"{', '.join(s.split(':')[0] for s in SKIPPED)}")
elif os.path.exists(SKIPPED_FILE):
    os.remove(SKIPPED_FILE)
