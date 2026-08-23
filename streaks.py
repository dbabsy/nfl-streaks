#!/usr/bin/env python3
"""
Active-streak scanner for NFL prop hunting.

Pulls game logs from ESPN's public API and writes a self-contained,
interactive HTML file. No dependencies beyond the standard library,
and no CORS to fight because the fetching happens here, not in a browser.

    python3 streaks.py                 # balanced QB/RB/WR pool, current season
    python3 streaks.py --pool 300      # wider net
    python3 streaks.py --season 2025   # the 2025 season
    python3 streaks.py --demo          # synthetic data, no network, for testing

With no --season the script asks ESPN what the current season is and falls
back to the previous one while the new season has no games yet.

Note the shape of the sport: a 17-game season caps every streak at 17, so
runs here are far shorter than the basketball or baseball equivalents.
"""

import argparse
import concurrent.futures as futures
import json
import random
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - missing tzdata; UTC dates are close enough
    EASTERN = None

SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl"
# site.api rejects browser-like agents with a 403; a curl-style one is accepted.
UA = {"User-Agent": "curl/8.7.1"}

# ESPN's own abbreviations. Used to screen exhibitions (Pro Bowl) out of the
# game logs — see load_teams(), which refreshes this from the API when it can.
NFL_TEAMS = frozenset(
    "ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LAC LAR LV "
    "MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WSH".split()
)

# One ranked request per position group, so the pool isn't all quarterbacks.
# Weights sum to 1 and split the requested pool size between the groups.
POOL_SORTS = [
    ("passing.passingYards:desc", 0.22),
    ("rushing.rushingYards:desc", 0.28),
    ("receiving.receivingYards:desc", 0.50),
]

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


# key, label, function of a game's stat dict -> bool.
# A player only ever charts in categories their position produces, so the
# quarterback rows drop out of "5+ Rec" on their own — no position filter
# needed, because a run of zero length is filtered out downstream.
CATS = [
    ("rec3", "3+ REC", lambda s: s["rec"] >= 3),
    ("rec5", "5+ REC", lambda s: s["rec"] >= 5),
    ("rec7", "7+ REC", lambda s: s["rec"] >= 7),
    ("recy50", "50+ REC YDS", lambda s: s["recy"] >= 50),
    ("recy75", "75+ REC YDS", lambda s: s["recy"] >= 75),
    ("recy100", "100+ REC YDS", lambda s: s["recy"] >= 100),
    ("ry50", "50+ RUSH YDS", lambda s: s["ry"] >= 50),
    ("ry75", "75+ RUSH YDS", lambda s: s["ry"] >= 75),
    ("ry100", "100+ RUSH YDS", lambda s: s["ry"] >= 100),
    ("scr50", "50+ SCRIM YDS", lambda s: s["ry"] + s["recy"] >= 50),
    ("scr100", "100+ SCRIM YDS", lambda s: s["ry"] + s["recy"] >= 100),
    ("td1", "1+ TD", lambda s: s["rtd"] + s["rectd"] >= 1),
    ("py200", "200+ PASS YDS", lambda s: s["py"] >= 200),
    ("py250", "250+ PASS YDS", lambda s: s["py"] >= 250),
    ("py300", "300+ PASS YDS", lambda s: s["py"] >= 300),
    ("ptd1", "1+ PASS TD", lambda s: s["ptd"] >= 1),
    ("ptd2", "2+ PASS TD", lambda s: s["ptd"] >= 2),
]


def get_json(url, tries=3):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25, context=SSL_CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def num(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def game_day(iso):
    """ESPN stamps tip-off in UTC, so an evening game reads as the next day.
    Convert to Eastern — the league's scheduling clock — so a Monday night
    game is dated Monday and doesn't collide with Tuesday's game."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (dt.astimezone(EASTERN) if EASTERN else dt).date().isoformat()
    except ValueError:
        return iso[:10]


def pool_url(season, limit, page=1, sort=None):
    params = {
        "region": "us", "lang": "en", "contentorigin": "espn",
        "season": season, "seasontype": 2, "limit": limit, "page": page,
    }
    if sort:
        params["sort"] = sort
    return f"{WEB}/statistics/byathlete?{urllib.parse.urlencode(params)}"


def resolve_season():
    """ESPN flips currentSeason to the upcoming year during the offseason;
    step back until we find a season that actually has games."""
    j = get_json(pool_url(2000, 1))  # any season returns the currentSeason block
    year = ((j.get("currentSeason") or {}).get("year")) or date.today().year
    for candidate in (year, year - 1):
        probe = get_json(pool_url(candidate, 1))
        if probe.get("athletes"):
            return candidate
    return year - 1


def season_label(season):
    return str(season)


def load_pool(season, size):
    """ESPN ranks byathlete on a single stat, so an unsorted request returns
    quarterbacks and nothing else. Pull one ranked slice per position group
    and merge, keeping each player's best rank."""
    rows = {}
    for sort, share in POOL_SORTS:
        want = max(int(size * share), 10)
        try:
            j = get_json(pool_url(season, want, sort=sort))
        except Exception as e:  # noqa: BLE001 - one group failing shouldn't sink the run
            print(f"  pool slice {sort.split('.')[0]} failed ({type(e).__name__})", flush=True)
            continue
        for rank, a in enumerate(j.get("athletes", [])):
            ath = a.get("athlete") or {}
            pid = ath.get("id")
            if not pid or str(pid) in rows:
                continue
            rows[str(pid)] = {
                "id": str(pid),
                "name": ath.get("displayName", ""),
                "team": ath.get("teamShortName", "") or "",
                "pos": ((ath.get("position") or {}).get("abbreviation") or ""),
                "rank": rank,
            }
    return sorted(rows.values(), key=lambda r: r["rank"])[:size]


def load_teams():
    """The 32 real franchises. Anything else showing up as an opponent is an
    exhibition (the Pro Bowl), so checking against this set keeps those out of
    the streaks. Falls back to the baked-in list if the endpoint is
    unavailable — a stale set beats no page at all."""
    try:
        j = get_json(f"{SITE}/teams", tries=2)
        found = {
            (t.get("team") or {}).get("abbreviation", "").upper()
            for league in j.get("sports", [{}])[0].get("leagues", [])
            for t in league.get("teams", [])
        } - {""}
        if len(found) >= 32:
            return found
        print(f"  teams endpoint returned {len(found)}; using built-in list", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  teams endpoint unavailable ({type(e).__name__}); using built-in list", flush=True)
    return set(NFL_TEAMS)


def load_log(pid, season, real_teams):
    j = get_json(f"{WEB}/athletes/{pid}/gamelog?season={season}")
    names, events = j.get("names", []), j.get("events", {})
    games = []
    for st in j.get("seasonTypes", []):
        if "Regular Season" not in (st.get("displayName") or ""):
            continue  # skip preseason / playoffs
        for cat in st.get("categories", []):
            for ev in cat.get("events", []):
                stats = ev.get("stats", [])
                # An inactive week logs as all dashes. A real game with a
                # quiet line is kept: catching nothing genuinely breaks a
                # streak, and dropping those would inflate every run.
                if not any(str(v).strip() not in ("", "-") for v in stats):
                    continue
                raw = dict(zip(names, stats))
                meta = events.get(str(ev.get("eventId"))) or {}
                opp = ((meta.get("opponent") or {}).get("abbreviation") or "")
                if opp.upper() not in real_teams:
                    continue  # Pro Bowl and other exhibitions
                games.append(
                    {
                        "ts": meta.get("gameDate") or "",
                        "date": game_day(meta.get("gameDate")),
                        "opp": opp,
                        "team": ((meta.get("team") or {}).get("abbreviation") or ""),
                        "st": {
                            "py": num(raw.get("passingYards")),
                            "ptd": num(raw.get("passingTouchdowns")),
                            "ry": num(raw.get("rushingYards")),
                            "rtd": num(raw.get("rushingTouchdowns")),
                            "rec": num(raw.get("receptions")),
                            "recy": num(raw.get("receivingYards")),
                            "rectd": num(raw.get("receivingTouchdowns")),
                        },
                    }
                )
    games.sort(key=lambda g: g["ts"])  # full timestamp: orders same-day games correctly
    return games


def encode(games):
    """One bitstring per category, oldest game first."""
    return {k: "".join("1" if f(g["st"]) else "0" for g in games) for k, _lab, f in CATS}


def build(season, pool_size, workers):
    print(f"season {season_label(season)} · fetching player pool …", flush=True)
    real_teams = load_teams()
    pool = load_pool(season, pool_size)
    if not pool:
        sys.exit(f"No stats returned for {season_label(season)}.")
    print(f"{len(pool)} players. pulling game logs …", flush=True)

    out, done = [], 0

    def work(p):
        games = load_log(p["id"], season, real_teams)
        if not games:
            return None
        # Label players by the roster they're on *now* — that's who you're
        # betting next game. When the streak was built somewhere else, say so
        # rather than letting a Celtics run sit silently under a Sixers logo.
        last_team = games[-1].get("team") or ""
        row = {
            "id": p["id"],
            "name": p["name"],
            "team": p["team"] or last_team,
            "pos": p.get("pos", ""),
            "flags": encode(games),
            "opps": [g["opp"] for g in games],
            "dates": [g["date"] for g in games],
        }
        if last_team and row["team"] and last_team != row["team"]:
            row["was"] = last_team
        return row

    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(lambda p: safe(work, p), pool):
            done += 1
            if done % 10 == 0 or done == len(pool):
                print(f"  {done}/{len(pool)}", end="\r", flush=True)
            if res:
                out.append(res)
    print()
    return out


def safe(fn, arg):
    try:
        return fn(arg)
    except Exception:  # noqa: BLE001
        return None


def demo_data():
    random.seed(7)
    names = [
        ("Ja'Marr Chase", "CIN", "WR"), ("Justin Jefferson", "MIN", "WR"),
        ("Puka Nacua", "LAR", "WR"), ("Amon-Ra St. Brown", "DET", "WR"),
        ("Brock Bowers", "LV", "TE"), ("Bijan Robinson", "ATL", "RB"),
        ("Saquon Barkley", "PHI", "RB"), ("Jahmyr Gibbs", "DET", "RB"),
        ("Josh Allen", "BUF", "QB"), ("Lamar Jackson", "BAL", "QB"),
        ("Joe Burrow", "CIN", "QB"), ("Patrick Mahomes", "KC", "QB"),
    ]
    rows = []
    for n, t, pos in names:
        games = []
        for _ in range(random.randint(14, 17)):
            qb, rb = pos == "QB", pos == "RB"
            games.append(
                {
                    "st": {
                        "py": max(0, int(random.gauss(255, 70))) if qb else 0,
                        "ptd": max(0, int(random.gauss(1.8, 1.1))) if qb else 0,
                        "ry": max(0, int(random.gauss(78, 34))) if rb else (
                            max(0, int(random.gauss(22, 18))) if qb else 0),
                        "rtd": (random.choice([0, 0, 1, 1, 2]) if rb else
                                random.choice([0, 0, 0, 1])),
                        "rec": 0 if qb else max(0, int(random.gauss(5 if pos != "RB" else 3, 2))),
                        "recy": 0 if qb else max(0, int(random.gauss(68 if pos != "RB" else 26, 30))),
                        "rectd": 0 if qb else random.choice([0, 0, 0, 1]),
                    },
                    "date": "2026-01-04",
                    "opp": "XXX",
                }
            )
        rows.append(
            {"id": "4362628", "name": n, "team": t, "pos": pos, "flags": encode(games),
             "opps": [x["opp"] for x in games], "dates": [x["date"] for x in games]}
        )
    return rows


# ── HTML ────────────────────────────────────────────────────────────────
TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NFL active streaks · __SEASON__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
<style>
:root{
  --ink:#0A1440; --panel:#122152; --panel2:#0D1838; --line:#22326E; --line2:#354A94;
  --text:#E7ECF9; --dim:#8C9AC7; --faint:#57649A; --amber:#FFB000; --amberDim:#7A5406;
  --hit:#31D07E; --miss:#FF4D6A;
  --mono:"Space Grotesk",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);
  font:400 14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;}
.wrap{max-width:1320px;margin:0 auto}
.topbar{position:sticky;top:0;z-index:5;background:var(--ink);box-shadow:0 1px 0 var(--line)}
body.capturing .topbar{position:static}
body.capturing .snap{visibility:hidden}
header{padding:20px 20px 14px}
.eyebrow{font-family:var(--mono);font-size:10px;font-weight:600;letter-spacing:.12em;color:var(--amber)}
h1{margin:2px 0 0;font-size:27px;font-weight:800;letter-spacing:-.035em;line-height:1.05}
.meta{font-family:var(--mono);font-size:10px;color:var(--faint);margin-top:6px;letter-spacing:.03em}
.controls{padding:12px 20px 14px;display:flex;flex-direction:column;gap:12px}
.pills{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;scrollbar-width:none}
.pills::-webkit-scrollbar{display:none}
.pill{flex:0 0 auto;padding:5px 12px;font-family:var(--mono);font-size:11px;font-weight:500;letter-spacing:.03em;
  text-transform:uppercase;color:var(--dim);background:var(--panel);border:1px solid var(--line);
  border-radius:100px;cursor:pointer;transition:border-color .12s,color .12s}
.pill:hover{border-color:var(--line2);color:var(--text)}
.pill[aria-pressed=true]{background:var(--amber);border-color:var(--amber);color:var(--ink);font-weight:700}
.snap{margin-left:auto;border-color:var(--amberDim);color:var(--amber);font-weight:600}
.snap:hover{border-color:var(--amber);background:rgba(255,176,0,.08)}
.snap:disabled{opacity:.55;cursor:default;border-color:var(--line)}
.row2{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.lbl{font-family:var(--mono);font-size:10px;color:var(--faint);letter-spacing:.05em}
.seg{display:flex;border-radius:6px;overflow:hidden}
.seg button{width:28px;height:26px;font-family:var(--mono);font-size:12px;font-weight:700;color:var(--dim);
  background:var(--panel);border:1px solid var(--line);margin-left:-1px;cursor:pointer}
.seg button:first-child{margin-left:0}
.seg button[aria-pressed=true]{background:var(--amber);border-color:var(--amber);color:var(--ink)}
input,select{height:28px;font-family:var(--mono);font-size:12px;color:var(--text);background:var(--panel);
  border:1px solid var(--line);border-radius:6px;padding:0 8px}
input.search{flex:1;min-width:160px;background:var(--panel2);border-radius:100px;padding:0 14px}
input.search:focus,input:focus{outline:1px solid var(--amberDim)}
.grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(370px,1fr));padding:8px 20px 20px}
.card{display:flex;align-items:center;gap:12px;padding:12px;background:var(--panel);
  border:1px solid var(--line);border-radius:12px;transition:border-color .12s,transform .12s}
.card:hover{border-color:var(--line2);transform:translateY(-1px)}
.rank{width:16px;text-align:right;font-family:var(--mono);font-size:9px;color:var(--faint);flex:0 0 auto}
.avatar{position:relative;flex:0 0 auto;width:56px;height:56px}
.ph{position:absolute;width:230%;left:-65%;top:-13%;display:block}
.shot{position:absolute;inset:0;border-radius:50%;overflow:hidden;
  background:var(--panel2);border:1px solid var(--line2)}
.ph-fb{position:absolute;inset:0;border-radius:50%;background:var(--panel2);border:1px solid var(--line2);
  display:none;align-items:center;justify-content:center;font-family:var(--mono);font-size:15px;
  font-weight:700;color:var(--dim)}
.lg{position:absolute;right:-3px;bottom:-3px;width:22px;height:22px;border-radius:50%;
  background:var(--ink);border:2px solid var(--ink);box-sizing:content-box;display:block}
.num{flex:0 0 auto;width:44px;height:40px;display:flex;align-items:center;justify-content:center;
  background:var(--panel2);border:1px solid var(--amberDim);color:var(--amber);border-radius:8px;
  font-family:var(--mono);font-size:22px;font-weight:700;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.body{flex:1;min-width:0}
.nm{font-size:13.5px;font-weight:650;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tm{font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:.04em;margin-left:8px}
.strip{display:flex;align-items:center;gap:8px;margin:6px 0}
.ghosts{display:flex;align-items:center;gap:2px;flex:0 0 auto;opacity:.65}
.ghosts i{width:4px;height:14px;border:1px solid var(--line2);display:block}
.ghosts i.f{border-color:#3D1220}
.wall{width:1px;height:16px;background:var(--line2);margin:0 2px}
.cells{display:flex;align-items:center;gap:2px;flex:1;min-width:0}
.cells b{flex:1;min-width:4px;height:14px;background:var(--hit);display:block;border-radius:2px}
.cells b.f{background:transparent;border:1.5px solid var(--miss)}
.more{font-family:var(--mono);font-size:9px;color:var(--faint);padding-right:4px}
.sub{display:flex;flex-wrap:wrap;gap:2px 12px;font-family:var(--mono);font-size:10px;color:var(--faint);
  font-variant-numeric:tabular-nums}
.sub em{font-style:normal;color:var(--dim)}
.sub .was{color:var(--amber);opacity:.8}
footer{padding:16px 20px;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:8px 20px;
  font-family:var(--mono);font-size:10px;color:var(--faint);align-items:center}
.key{display:inline-flex;align-items:center;gap:6px}
.key s{width:8px;height:12px;background:var(--hit);display:inline-block;text-decoration:none;border-radius:2px}
.key s.f{background:transparent;border:1.5px solid var(--miss)}
.empty{padding:16px 20px;font-family:var(--mono);font-size:12px;color:var(--faint)}
</style></head><body><div class="wrap">
<div class="topbar">
<header>
  <div class="eyebrow">NFL · ACTIVE STREAKS</div>
  <h1>Longest runs, breaks allowed</h1>
  <div class="meta">__SEASON__ regular season · __N__ players · built __BUILT__</div>
</header>
<div class="controls">
  <div class="pills" id="cats"></div>
  <div class="row2">
    <span class="lbl">BREAKS</span>
    <div class="seg" id="breaks"></div>
    <span class="lbl">MIN G</span><input id="ming" type="number" value="8" min="1" style="width:52px">
    <span class="lbl">SEARCH</span><input id="q" class="search" placeholder="player, team, or position">
    <button id="shot" class="pill snap" type="button">Save image</button>
  </div>
</div>
</div>
<div class="grid" id="grid"></div>
<div class="empty" id="empty" hidden>No players match. Lower MIN G or clear the search.</div>
<footer>
  <span class="key"><s></s>success</span>
  <span class="key"><s class="f"></s>break</span>
  <span class="key"><i style="width:4px;height:12px;border:1px solid var(--line2);display:inline-block"></i>
    <i style="width:1px;height:12px;background:var(--line2);display:inline-block"></i> games that closed the window</span>
  <span>most recent on the right · inactive weeks excluded</span>
  <span><em>clean</em> = same streak at 0 breaks</span>
  <span><em>season</em> = full-year rate, shown as no-vig American price</span>
  <span>data: ESPN</span>
</footer></div>
<script>
const DATA = __DATA__;
const CATS = __CATS__;
let cat = "rec3", breaks = 1;

function win(s, k){ let f=0, i=s.length-1;
  for(; i>=0; i--){ if(s[i]==="0"){ f++; if(f>k) break; } }
  return {start:i+1, len:s.length-i-1, wall:i}; }
function amer(p){ if(!(p>0)||p>=1) return "—";
  return p>=0.5 ? "-"+Math.round(100*p/(1-p)) : "+"+Math.round(100*(1-p)/p); }
function initials(n){ return n.split(/\s+/).filter(Boolean).map(w=>w[0]).slice(0,2).join("").toUpperCase(); }
function headshot(id){ return `https://a.espncdn.com/i/headshots/nfl/players/full/${id}.png`; }
function logo(team){ return `https://a.espncdn.com/i/teamlogos/nfl/500/${team.toLowerCase()}.png`; }

function render(){
  const q = document.getElementById("q").value.trim().toLowerCase();
  const ming = Math.max(1, +document.getElementById("ming").value || 1);
  const rows = DATA.map(p=>{
    const s = p.flags[cat]||"";
    const w = win(s, breaks), c = win(s, 0);
    const made = (s.match(/1/g)||[]).length;
    const inrun = ((s.slice(w.start).match(/1/g))||[]).length;
    return {...p, s, ...w, clean:c.len, inrun, g:s.length, rate:s.length?made/s.length:0};
  }).filter(p=> p.g>=ming && p.len>0 &&
      (!q || p.name.toLowerCase().includes(q) || p.team.toLowerCase().includes(q)))
    .sort((a,b)=> b.len-a.len || b.rate-a.rate);

  const g = document.getElementById("grid");
  document.getElementById("empty").hidden = rows.length>0;
  g.innerHTML = rows.map((p,i)=>{
    const MAX=32, w=p.s.slice(p.start), hid=Math.max(0,w.length-MAX), sh=w.slice(hid);
    const gh=p.s.slice(Math.max(0,p.wall-2), p.wall+1);
    return `<div class="card">
      <span class="rank">${i+1}</span>
      <div class="avatar">
        <span class="shot"><img class="ph" src="${headshot(p.id)}" alt="" loading="lazy" crossorigin="anonymous"
          onerror="this.onerror=null;this.parentElement.style.display='none';this.parentElement.nextElementSibling.style.display='flex';"></span>
        <span class="ph-fb">${initials(p.name)}</span>
        <img class="lg" src="${logo(p.team)}" alt="" loading="lazy" crossorigin="anonymous" onerror="this.style.display='none'">
      </div>
      <div class="num">${p.len}</div>
      <div class="body">
        <div class="nm">${p.name}<span class="tm">${p.team}</span></div>
        <div class="strip">
          ${gh ? `<span class="ghosts">${[...gh].map(c=>`<i class="${c==="1"?"":"f"}"></i>`).join("")}</span><span class="wall"></span>`:""}
          <span class="cells">${hid?`<span class="more">+${hid}</span>`:""}${[...sh].map(c=>`<b class="${c==="1"?"":"f"}"></b>`).join("")}</span>
        </div>
        <div class="sub"><span>${p.inrun}/${p.len} in run</span><span>clean ${p.clean}</span>
          <span>season ${(p.rate*100).toFixed(0)}% <em>${amer(p.rate)}</em></span>
          <span>${CATS.find(c=>c[0]===cat)[1]}</span>
          ${p.was?`<span class="was">streak w/ ${p.was}</span>`:""}</div>
      </div></div>`;
  }).join("");
}

const cp = document.getElementById("cats");
CATS.forEach(([k,l])=>{ const b=document.createElement("button");
  b.className="pill"; b.textContent=l; b.setAttribute("aria-pressed", k===cat);
  b.onclick=()=>{ cat=k; [...cp.children].forEach(x=>x.setAttribute("aria-pressed", x===b)); render(); };
  cp.appendChild(b); });
const bp = document.getElementById("breaks");
[0,1,2,3].forEach(n=>{ const b=document.createElement("button");
  b.textContent=n; b.setAttribute("aria-pressed", n===breaks);
  b.onclick=()=>{ breaks=n; [...bp.children].forEach(x=>x.setAttribute("aria-pressed", x===b)); render(); };
  bp.appendChild(b); });
document.getElementById("q").oninput = render;
document.getElementById("ming").oninput = render;

document.getElementById("shot").onclick = async () => {
  const btn = document.getElementById("shot");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Capturing…";
  document.body.classList.add("capturing");
  try {
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
    const ink = getComputedStyle(document.documentElement).getPropertyValue("--ink").trim() || "#0A1440";
    const canvas = await html2canvas(document.querySelector(".wrap"), {
      backgroundColor: ink, scale: 2, useCORS: true,
    });
    const catLabel = (CATS.find(c => c[0] === cat) || [, "streak"])[1].toLowerCase().replace(/[^a-z0-9]+/g, "-");
    const stamp = new Date().toISOString().slice(0, 10);
    const a = document.createElement("a");
    a.download = `nfl-streaks-${catLabel}-${breaks}breaks-${stamp}.png`;
    a.href = canvas.toDataURL("image/png");
    a.click();
  } catch (e) {
    console.error(e);
    alert("Couldn't capture the screenshot: " + e.message);
  } finally {
    document.body.classList.remove("capturing");
    btn.disabled = false; btn.textContent = label;
  }
};

render();
</script></body></html>
"""


def write_html(rows, season, out_path):
    html = (
        TEMPLATE.replace("__DATA__", json.dumps(rows, separators=(",", ":")))
        .replace("__CATS__", json.dumps([[k, lab] for k, lab, _ in CATS]))
        .replace("__SEASON__", season_label(season))
        .replace("__N__", str(len(rows)))
        .replace("__BUILT__", date.today().isoformat())
    )
    Path(out_path).write_text(html, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None,
                    help="ESPN season year; 2026 = the 2025-26 season")
    ap.add_argument("--pool", type=int, default=150, help="top N players by minutes")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="streaks.html")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if a.demo:
        rows, season = demo_data(), a.season or 2026
    else:
        season = a.season or resolve_season()
        rows = build(season, a.pool, a.workers)
    if not rows:
        sys.exit("Nothing to write.")
    p = write_html(rows, season, a.out)
    print(f"wrote {p}  ({len(rows)} players, {Path(p).stat().st_size // 1024} KB)")
    if not a.no_open:
        webbrowser.open(Path(p).resolve().as_uri())


if __name__ == "__main__":
    main()
