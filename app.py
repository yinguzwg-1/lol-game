# -*- coding: utf-8 -*-
"""5v5 均衡抽签 - Flask 共享版
玩家池与积分存服务器（SQLite），微信群/局域网内所有人看到同一份数据。
"""
import os
import re
import json
import math
import time
import random
import sqlite3
import gzip as _gzip
import threading
import datetime
import uuid
import subprocess
import tempfile
from urllib.parse import urlsplit
from io import BytesIO
from flask import Flask, request, jsonify, send_file

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'elo_draw.db')
INDEX = os.path.join(BASE, 'index.html')
PORT = 9529

app = Flask(__name__)


# ---------- 流量优化：gzip 压缩 + 浏览器缓存（每月 1GB 花生壳流量的关键） ----------
@app.after_request
def _gzip_and_cache(resp):
    """HTML/JSON/manifest 自动 gzip（约省 75% 流量）；图标缓存 7 天（重复访问零流量）。
    不压缩 PNG（已压缩格式，压不动还耗 CPU）。异常时原样返回，不影响功能。"""
    try:
        if resp.status_code == 200:
            ct = resp.headers.get('Content-Type') or ''
            # 1) 缓存策略：图标与 manifest 缓存 7 天；HTML 每次校验（304 只需几百字节）
            if ('image/png' in ct) or ('manifest' in ct):
                resp.headers['Cache-Control'] = 'public, max-age=604800'
            elif 'text/html' in ct:
                resp.headers['Cache-Control'] = 'no-cache'
            # 2) gzip 压缩（客户端支持且响应大于 512 字节时）
            compressible = ('text/html' in ct) or ('application/json' in ct) \
                or ('manifest' in ct) or ('javascript' in ct) or ('text/plain' in ct)
            if compressible and 'gzip' in (request.headers.get('Accept-Encoding') or '').lower() \
                    and not resp.headers.get('Content-Encoding') \
                    and not resp.headers.get('Content-Range') \
                    and request.method != 'HEAD':
                # send_file 等直传（direct_passthrough）响应需先迭代取回字节，
                # 并立即 set_data 写回——否则未达压缩阈值的响应体会被掏空
                # （body 为空但 Content-Length 不变，浏览器报 ERR_CONTENT_LENGTH_MISMATCH）
                if resp.direct_passthrough:
                    data = b''.join(resp.response)
                    resp.direct_passthrough = False
                    resp.set_data(data)
                else:
                    data = resp.get_data()
                if data and len(data) >= 512:
                    buf = BytesIO()
                    with _gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6) as f:
                        f.write(data)
                    gz = buf.getvalue()
                    if len(gz) < len(data):
                        resp.set_data(gz)
                        resp.headers['Content-Encoding'] = 'gzip'
                        resp.headers['Content-Length'] = str(len(gz))
                        resp.headers['Vary'] = 'Accept-Encoding'
    except Exception:
        pass
    return resp

# 首次启动导入的初始玩家（11人 + 昨日五场 5v5 结算积分，卢本伟未参赛 0 分）
INITIAL_PLAYERS = [
    {'name': 'UZI', 'score5': 5},
    {'name': '神撞大队长', 'score5': 3},
    {'name': '方特永远的神QvQ', 'score5': 1},
    {'name': 'The 棍', 'score5': 1},
    {'name': '卢本伟', 'score5': 0},
    {'name': '米兰的小铁锤', 'score5': -1},
    {'name': '秒退道士', 'score5': -1},
    {'name': 'faker', 'score5': -1},
    {'name': 'OttO', 'score5': -1},
    {'name': '抑郁快滚开', 'score5': -3},
    {'name': 'ZEUS', 'score5': -3},
]


def score_col(n):
    """每队人数 3/4/5 → 对应积分列名"""
    return 'score%d' % n


# 积分规则：胜 +5 / 负 -5；四杀标记：胜方全员 +1 / 败方全员 −1；
# 五杀标记（仅 5v5）：胜方全员 +2 / 败方全员 −2（同人可多次标记，可叠加）
WIN_SCORE = 5
LOSE_SCORE = 5
KILL_BONUS_QUADRA = 1   # 四杀加成
KILL_BONUS_PENTA = 2    # 五杀加成


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS players(
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE,
        score3 INTEGER DEFAULT 0,
        score4 INTEGER DEFAULT 0,
        score5 INTEGER DEFAULT 0,
        delta INTEGER DEFAULT 0)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time TEXT,
        team_a TEXT,
        team_b TEXT,
        result TEXT DEFAULT NULL,
        draw_type TEXT DEFAULT 'balanced',
        lol_game_id INTEGER)''')
    # ---- 旧库迁移：补列 ----
    pcols = [r['name'] for r in conn.execute('PRAGMA table_info(players)').fetchall()]
    if 'score3' not in pcols:
        conn.execute('ALTER TABLE players ADD COLUMN score3 INTEGER DEFAULT 0')
    if 'score4' not in pcols:
        conn.execute('ALTER TABLE players ADD COLUMN score4 INTEGER DEFAULT 0')
    if 'score5' not in pcols:
        conn.execute('ALTER TABLE players ADD COLUMN score5 INTEGER DEFAULT 0')
        if 'score' in pcols:   # 旧单一积分表 5v5 积分迁到 score5
            conn.execute('UPDATE players SET score5 = score')
    gcols = [r['name'] for r in conn.execute('PRAGMA table_info(games)').fetchall()]
    if 'draw_type' not in gcols:
        conn.execute("ALTER TABLE games ADD COLUMN draw_type TEXT DEFAULT 'balanced'")
    if 'kill_ids' not in gcols:
        conn.execute("ALTER TABLE games ADD COLUMN kill_ids TEXT DEFAULT '[]'")
    if 'penta_ids' not in gcols:   # 五杀标记（仅 5v5，独立于 kill_ids 四杀标记）
        conn.execute("ALTER TABLE games ADD COLUMN penta_ids TEXT DEFAULT '[]'")
    if 'lol_game_id' not in gcols:   # LOL 自动导入的对局唯一编号，用于幂等去重
        conn.execute("ALTER TABLE games ADD COLUMN lol_game_id INTEGER")
    # 智能分析存档：按比赛日存一份四榜单（导入/录入/修改/撤销后自动重算，凌晨 3 点定版）
    conn.execute('''CREATE TABLE IF NOT EXISTS analytics(
        day TEXT PRIMARY KEY,
        games_count INTEGER DEFAULT 0,
        content TEXT DEFAULT '[]',
        generated_at TEXT)''')
    # 当前共享抽签分组：抽签结果展示 + 重抽投票（所有人可见，5 分钟失效，只保留最新一条）
    conn.execute('''CREATE TABLE IF NOT EXISTS current_draw(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        draw_type TEXT,
        mode INTEGER,
        team_a TEXT,
        team_b TEXT,
        votes TEXT DEFAULT '[]',
        created_at REAL)''')
    # 最近一次抽签的参赛玩家名单（持久，不随分组失效清除；供下次打开恢复勾选，全群共享）
    conn.execute('''CREATE TABLE IF NOT EXISTS last_draw(
        id INTEGER PRIMARY KEY CHECK(id=1),
        player_ids TEXT,
        updated_at REAL)''')
    # 一次性换算：旧规则 ±1 → 新规则 ±5，历史积分×5（幂等，只执行一次）
    conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
    flag = conn.execute("SELECT value FROM meta WHERE key='rescale_5x_v1'").fetchone()
    if not flag:
        conn.execute('UPDATE players SET score3=score3*5, score4=score4*5, score5=score5*5')
        conn.execute("INSERT INTO meta(key,value) VALUES('rescale_5x_v1','1')")
    # 一次性迁移：五杀加档（5v5 五杀 ±2）。旧数据里 5v5 的 kill_ids 语义是五杀标记（当时按 ±1 计），
    # 迁到 penta_ids 新列，并按新规则补足差额（每人 ±1/标记）；4v4 的 kill_ids 保持四杀 ±1 不变
    flag2 = conn.execute("SELECT value FROM meta WHERE key='penta_tier_v1'").fetchone()
    if not flag2:
        for r in conn.execute('SELECT id, team_a, team_b, result, kill_ids FROM games').fetchall():
            try:
                ta = json.loads(r['team_a'])
            except Exception:
                ta = []
            if len(ta) != 5 or not r['result']:
                continue
            kl = json.loads(r['kill_ids'] or '[]')
            if not kl:
                continue
            tb = json.loads(r['team_b'])
            win = ta if r['result'] == 'A' else tb
            lose = tb if r['result'] == 'A' else ta
            n_old = len(kl)
            diff = n_old * (KILL_BONUS_PENTA - KILL_BONUS_QUADRA)   # 每人补足差额
            for p in win:
                conn.execute('UPDATE players SET score5=score5+? WHERE id=?', (diff, p['id']))
            for p in lose:
                conn.execute('UPDATE players SET score5=score5-? WHERE id=?', (diff, p['id']))
            conn.execute('UPDATE games SET penta_ids=?, kill_ids="[]" WHERE id=?',
                         (json.dumps(kl), r['id']))
        conn.execute("INSERT INTO meta(key,value) VALUES('penta_tier_v1','1')")
    cur = conn.execute('SELECT COUNT(*) AS c FROM players')
    if cur.fetchone()['c'] == 0:
        for i, p in enumerate(INITIAL_PLAYERS):
            conn.execute('INSERT OR IGNORE INTO players(id,name,score3,score4,score5,delta) VALUES(?,?,0,0,?,?)',
                         ('p_init_%d' % i, p['name'], p.get('score5', 0), 0))
    conn.commit()
    conn.close()


# ---------- 页面 ----------
@app.route('/')
def index():
    return send_file(INDEX)


# ---------- PWA（添加到手机桌面：应用名 + 图标 + 独立窗口） ----------
@app.route('/manifest.webmanifest')
def manifest_file():
    return send_file(os.path.join(BASE, 'manifest.webmanifest'),
                     mimetype='application/manifest+json')


@app.route('/icon-<int:size>.png')
def icon_file(size):
    if size not in (192, 512):
        return jsonify({'ok': False}), 404
    return send_file(os.path.join(BASE, 'icon-%d.png' % size), mimetype='image/png')


@app.route('/apple-touch-icon.png')
def apple_icon():
    return send_file(os.path.join(BASE, 'apple-touch-icon.png'), mimetype='image/png')


# ---------- 状态 ----------
@app.route('/api/state')
def api_state():
    conn = get_db()
    players = [dict(r) for r in conn.execute('SELECT * FROM players').fetchall()]
    games = []
    for r in conn.execute('SELECT * FROM games ORDER BY id').fetchall():
        gk = r['kill_ids'] if 'kill_ids' in r.keys() else None
        pk = r['penta_ids'] if 'penta_ids' in r.keys() else None
        games.append({
            'id': r['id'],
            'time': r['time'],
            'teamA': json.loads(r['team_a']),
            'teamB': json.loads(r['team_b']),
            'result': r['result'],
            'draw_type': r['draw_type'] if 'draw_type' in r.keys() else 'balanced',
            'kill_ids': json.loads(gk) if gk else [],
            'penta_ids': json.loads(pk) if pk else [],
            'lol_game_id': r['lol_game_id'] if 'lol_game_id' in r.keys() else None,
        })
    # 当前共享抽签分组（所有人刷新可见；5 分钟失效；含重抽票数与阈值）
    draw = None
    drow = conn.execute('SELECT * FROM current_draw ORDER BY id DESC LIMIT 1').fetchone()
    if drow:
        age = time.time() - (drow['created_at'] or 0)
        if age <= DRAW_TTL:
            dta = json.loads(drow['team_a'])
            draw = {
                'draw_type': drow['draw_type'],
                'mode': drow['mode'],
                'teamA': dta,
                'teamB': json.loads(drow['team_b']),
                'votes_count': len(json.loads(drow['votes'] or '[]')),
                'threshold': len(dta),
                'age': age,
            }
        else:
            conn.execute('DELETE FROM current_draw')
            conn.commit()
    # 最近一次抽签的参赛玩家名单（持久，全群共享，下次打开恢复勾选用）
    last_pick = []
    lrow = conn.execute('SELECT player_ids FROM last_draw WHERE id=1').fetchone()
    if lrow and lrow['player_ids']:
        last_pick = json.loads(lrow['player_ids'])
    conn.close()
    return jsonify({'players': players, 'games': games, 'draw': draw, 'last_pick': last_pick})


# ---------- 当前共享抽签分组（所有人可见，5 分钟失效；达标重抽由服务器执行，多端一致） ----------
DRAW_TTL = 5 * 60


def _draw_balanced(pool):
    """均衡配对蛇形分组（与前端 balancedDraw 同规则）：按积分降序两两配对，每轮总分低的队先拿高顺位"""
    sp = sorted(pool, key=lambda p: p.get('score', 0), reverse=True)
    teams = ([], [])
    sums = [0, 0]
    for i in range(0, len(sp), 2):
        first = 0 if sums[0] <= sums[1] else 1
        hi = sp[i]
        lo = sp[i + 1] if i + 1 < len(sp) else None
        teams[first].append(hi)
        sums[first] += hi.get('score', 0)
        if lo is not None:
            teams[1 - first].append(lo)
            sums[1 - first] += lo.get('score', 0)
    for t in teams:
        random.shuffle(t)
    return teams[0], teams[1]


def _draw_random(pool):
    """纯随机：洗牌后对半分（与前端 randomDraw 同规则）"""
    allp = list(pool)
    random.shuffle(allp)
    n = len(allp) // 2
    return allp[:n], allp[n:]


@app.route('/api/draw/submit', methods=['POST'])
def api_draw_submit():
    """抽签发起设备提交分组结果，存为全组共享的当前抽签（覆盖旧的，票数清零，重新计 5 分钟时效）"""
    data = request.get_json(force=True)
    dtype = data.get('drawType') or 'balanced'
    if dtype not in ('balanced', 'random'):
        dtype = 'balanced'
    ta, tb = data.get('teamA') or [], data.get('teamB') or []
    n = len(ta)
    if not (3 <= n <= 5) or len(tb) != n:
        return jsonify({'ok': False, 'msg': '两队人数需相同且 3-5 人'})
    conn = get_db()
    ids = {r['id'] for r in conn.execute('SELECT id FROM players').fetchall()}
    for m in list(ta) + list(tb):
        if not isinstance(m, dict) or m.get('id') not in ids:
            conn.close()
            return jsonify({'ok': False, 'msg': '有队员不在玩家池：' + str((m or {}).get('name', '?'))})
    conn.execute('DELETE FROM current_draw')
    conn.execute(
        'INSERT INTO current_draw(draw_type, mode, team_a, team_b, votes, created_at) VALUES(?,?,?,?,?,?)',
        (dtype, n, json.dumps(ta, ensure_ascii=False), json.dumps(tb, ensure_ascii=False), '[]', time.time()))
    # 持久记录本次抽签参赛名单（全群共享，下次打开恢复勾选用）
    player_ids = [str(m['id']) for m in list(ta) + list(tb)]
    conn.execute('INSERT OR REPLACE INTO last_draw(id, player_ids, updated_at) VALUES(1,?,?)',
                 (json.dumps(player_ids, ensure_ascii=False), time.time()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/draw/vote', methods=['POST'])
def api_draw_vote():
    """双击分组卡片投重抽票（每台设备一票）；达半数（5v5 为 5 票）后解散当前分组——
    所有人回到选人界面重新勾选抽签（不再由服务器直接重抽），返回 met=true 前端提示"""
    data = request.get_json(force=True)
    voter = str(data.get('voter') or '')
    if not voter:
        return jsonify({'ok': False, 'msg': '缺少 voter'})
    conn = get_db()
    row = conn.execute('SELECT * FROM current_draw ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'msg': '当前没有抽签分组'})
    if time.time() - (row['created_at'] or 0) > DRAW_TTL:
        conn.execute('DELETE FROM current_draw')
        conn.commit()
        conn.close()
        return jsonify({'ok': False, 'msg': '抽签分组已过期（超过 5 分钟），请重新抽签'})
    ta = json.loads(row['team_a'])
    tb = json.loads(row['team_b'])
    threshold = len(ta)          # 5v5→5 票（10 人的一半）、4v4→4、3v3→3
    votes = json.loads(row['votes'] or '[]')
    met = False
    if voter not in votes:
        votes.append(voter)
        if len(votes) >= threshold:
            # 达标：解散当前分组，回到选人界面（选人勾选仍保留在各端，重新点「开始抽签」即可）
            conn.execute('DELETE FROM current_draw')
            met = True
        else:
            conn.execute('UPDATE current_draw SET votes=? WHERE id=?', (json.dumps(votes), row['id']))
        conn.commit()
    conn.close()
    return jsonify({'ok': True, 'votes': len(votes), 'threshold': threshold, 'met': met})


@app.route('/api/draw', methods=['DELETE'])
def api_draw_delete():
    """手动删除当前抽签分组（全组同步消失）"""
    conn = get_db()
    conn.execute('DELETE FROM current_draw')
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ---------- 玩家 ----------
@app.route('/api/players', methods=['POST'])
def api_add_player():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'msg': '请输入玩家昵称'})
    conn = get_db()
    if conn.execute('SELECT 1 FROM players WHERE name=?', (name,)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'msg': '该昵称已存在'})
    pid = 'p%d' % int(time.time() * 1000)
    conn.execute('INSERT INTO players(id,name,score3,score4,score5,delta) VALUES(?,?,0,0,0,0)', (pid, name))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/players/<pid>', methods=['DELETE'])
def api_del_player(pid):
    conn = get_db()
    conn.execute('DELETE FROM players WHERE id=?', (pid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ---------- 对局 ----------
@app.route('/api/games', methods=['POST'])
def api_create_game():
    data = request.get_json(force=True)
    team_a = data.get('teamA', [])
    team_b = data.get('teamB', [])
    n = len(team_a)
    # 支持 3v3 / 4v4 / 5v5：每队 3-5 人且两队人数相同
    if n < 3 or n > 5 or n != len(team_b):
        return jsonify({'ok': False, 'msg': '每队需 3-5 人且两队人数相同'})
    draw_type = data.get('drawType') or data.get('draw_type') or 'balanced'
    if draw_type not in ('balanced', 'random'):
        draw_type = 'balanced'
    now = time.strftime('%m-%d %H:%M')
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO games(time,team_a,team_b,result,draw_type) VALUES(?,?,?,NULL,?)',
        (now, json.dumps(team_a, ensure_ascii=False), json.dumps(team_b, ensure_ascii=False), draw_type))
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': gid})


@app.route('/api/games/<int:gid>/result', methods=['POST'])
def api_set_result(gid):
    """录入或修改对局结果：胜 +5 / 败 -5，计入对应模式榜；
    四杀标记：胜方全员 +1、败方全员 -1（4v4 / 5v5 均可）；
    五杀标记（仅 5v5）：胜方全员 +2、败方全员 -2（同人可多次标记，可叠加）；
    修改时先回退旧结果再应用新结果（幂等）"""
    data = request.get_json(force=True)
    result = data.get('result')
    kill_ids_new = data.get('killIds') or []
    penta_ids_new = data.get('pentaIds') or []
    if result not in ('A', 'B'):
        return jsonify({'ok': False, 'msg': '无效结果'})
    conn = get_db()
    row = conn.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'msg': '对局不存在'})
    team_a = json.loads(row['team_a'])
    team_b = json.loads(row['team_b'])
    n = len(team_a)
    col = score_col(n)                    # 该场模式的积分列
    allow_quadra = n in (4, 5)            # 四杀：4v4 / 5v5 都可标记
    allow_penta = n == 5                  # 五杀：仅 5v5
    gk = row['kill_ids'] if 'kill_ids' in row.keys() else None
    pk = row['penta_ids'] if 'penta_ids' in row.keys() else None
    old_kill = json.loads(gk) if (allow_quadra and gk) else []
    old_penta = json.loads(pk) if (allow_penta and pk) else []
    old = row['result']
    # 回退旧结果（含旧加成：四杀 ±1 / 五杀 ±2 分级回退）
    if old in ('A', 'B'):
        old_win = team_a if old == 'A' else team_b
        old_lose = team_b if old == 'A' else team_a
        for p in old_win:
            conn.execute('UPDATE players SET %s=%s-? WHERE id=?' % (col, col), (WIN_SCORE, p['id']))
        for p in old_lose:
            conn.execute('UPDATE players SET %s=%s+? WHERE id=?' % (col, col), (LOSE_SCORE, p['id']))
        old_win_ids = {p['id'] for p in old_win}
        nq_old = len([k for k in old_kill if k in old_win_ids])    # 旧四杀标记次数
        np_old = len([k for k in old_penta if k in old_win_ids])   # 旧五杀标记次数
        bonus_old = nq_old * KILL_BONUS_QUADRA + np_old * KILL_BONUS_PENTA
        if bonus_old:
            for p in old_win:
                conn.execute('UPDATE players SET %s=%s-? WHERE id=?' % (col, col), (bonus_old, p['id']))
            for p in old_lose:
                conn.execute('UPDATE players SET %s=%s+? WHERE id=?' % (col, col), (bonus_old, p['id']))
    # 应用新结果：胜方每人 +5 + 加成、败方每人 -5 - 加成
    # 加成 = 四杀次数×1 + 五杀次数×2（仅胜方成员的标记有效，同一人可重复标记叠加）
    win = team_a if result == 'A' else team_b
    lose = team_b if result == 'A' else team_a
    win_ids = {p['id'] for p in win}
    lose_ids = {p['id'] for p in lose}
    quadra_list = [pid for pid in kill_ids_new if allow_quadra and pid in win_ids]   # 存库：id 重复出现 = 同人多次
    penta_list = [pid for pid in penta_ids_new if allow_penta and pid in win_ids]
    n_kill = len(quadra_list) + len(penta_list)
    bonus_total = len(quadra_list) * KILL_BONUS_QUADRA + len(penta_list) * KILL_BONUS_PENTA
    for pr in conn.execute('SELECT * FROM players').fetchall():
        delta = 0
        if pr['id'] in win_ids:
            delta = WIN_SCORE + bonus_total                 # 胜方全员 +5 + 加成
        elif pr['id'] in lose_ids:
            delta = -LOSE_SCORE - bonus_total               # 败方全员 -5 - 加成
        conn.execute('UPDATE players SET %s=%s+?, delta=? WHERE id=?' % (col, col), (delta, delta, pr['id']))
    conn.execute('UPDATE games SET result=?, kill_ids=?, penta_ids=? WHERE id=?',
                 (result, json.dumps(quadra_list), json.dumps(penta_list), gid))
    conn.commit()
    conn.close()
    regen_day(game_day_key(row['time']))   # 录入/修改后自动重算该比赛日的智能分析
    return jsonify({'ok': True})


@app.route('/api/games/<int:gid>/undo', methods=['POST'])
def api_undo_game(gid):
    conn = get_db()
    row = conn.execute('SELECT * FROM games WHERE id=?', (gid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'msg': '对局不存在'})
    if row['result']:
        team_a = json.loads(row['team_a'])
        team_b = json.loads(row['team_b'])
        col = score_col(len(team_a))       # 按该场模式回退积分
        win = team_a if row['result'] == 'A' else team_b
        lose = team_b if row['result'] == 'A' else team_a
        for p in win:
            conn.execute('UPDATE players SET %s=%s-? WHERE id=?' % (col, col), (WIN_SCORE, p['id']))
        for p in lose:
            conn.execute('UPDATE players SET %s=%s+? WHERE id=?' % (col, col), (LOSE_SCORE, p['id']))
        if len(team_a) in (4, 5):           # 回退四杀/五杀全队加成（四杀 ±1 / 五杀 ±2 分级回退）
            gk = row['kill_ids'] if 'kill_ids' in row.keys() else None
            pk = row['penta_ids'] if 'penta_ids' in row.keys() else None
            kill_list = json.loads(gk) if gk else []
            penta_list = json.loads(pk) if (len(team_a) == 5 and pk) else []
            win_ids = {p['id'] for p in win}
            nq = len([k for k in kill_list if k in win_ids])
            npk = len([k for k in penta_list if k in win_ids])
            bonus = nq * KILL_BONUS_QUADRA + npk * KILL_BONUS_PENTA
            if bonus:
                for p in win:
                    conn.execute('UPDATE players SET %s=%s-? WHERE id=?' % (col, col), (bonus, p['id']))
                for p in lose:
                    conn.execute('UPDATE players SET %s=%s+? WHERE id=?' % (col, col), (bonus, p['id']))
    conn.execute('DELETE FROM games WHERE id=?', (gid,))
    conn.commit()
    conn.close()
    regen_day(game_day_key(row['time']))   # 撤销后自动重算（该日无局则删档）
    return jsonify({'ok': True})


# ---------- 名字归一化（LOL Riot ID 可能用繁体，如“錘/锤”） ----------
TS_MAP = {
    '錘': '锤', '龍': '龙', '愛': '爱', '劍': '剑', '戰': '战', '鳳': '凤', '雲': '云', '風': '风', '電': '电',
    '誅': '诛', '殺': '杀', '夢': '梦', '靈': '灵', '聖': '圣', '斬': '斩', '絕': '绝', '蒼': '苍', '諾': '诺',
    '賽': '赛', '亞': '亚', '倫': '伦', '爾': '尔', '維': '维', '銀': '银', '鈴': '铃', '蘭': '兰', '麗': '丽',
    '華': '华', '強': '强', '偉': '伟', '傑': '杰', '鷹': '鹰', '嶽': '岳', '濤': '涛', '無': '无', '聽': '听',
    '語': '语', '話': '话', '讀': '读', '書': '书', '筆': '笔', '詩': '诗', '癲': '癫', '瘋': '疯', '閑': '闲',
    '開': '开', '閉': '闭', '關': '关', '門': '门', '戶': '户', '廢': '废', '張': '张', '歸': '归', '當': '当',
    '發': '发', '藍': '蓝', '紅': '红', '綠': '绿', '黃': '黄', '陽': '阳', '陰': '阴', '輝': '辉', '燦': '灿',
    '爛': '烂', '極': '极', '終': '终', '寶': '宝', '貝': '贝', '貴': '贵', '買': '买', '賣': '卖', '贏': '赢',
    '輸': '输', '雙': '双', '單': '单', '歲': '岁', '貓': '猫', '鳥': '鸟', '魚': '鱼', '蟲': '虫', '馬': '马',
    '雞': '鸡', '獸': '兽', '獅': '狮', '烏': '乌', '鶴': '鹤', '燈': '灯', '煙': '烟', '塵': '尘', '澤': '泽',
    '島': '岛', '嶺': '岭', '鐵': '铁', '鋼': '钢', '銅': '铜', '鑽': '钻', '環': '环', '錦': '锦', '繡': '绣',
    '綢': '绸', '緞': '缎', '紗': '纱', '絨': '绒', '劉': '刘', '陳': '陈', '楊': '杨', '趙': '赵', '吳': '吴',
    '孫': '孙', '許': '许', '韓': '韩', '馮': '冯', '鄧': '邓', '蕭': '萧', '蔣': '蒋', '葉': '叶', '蘇': '苏',
    '魏': '魏', '呂': '吕', '盧': '卢', '鐘': '钟', '譚': '谭', '陸': '陆', '萬': '万', '錢': '钱', '嚴': '严',
    '賀': '贺', '顧': '顾', '龔': '龚', '為': '为', '與': '与', '於': '于', '衛': '卫', '圍': '围', '領': '领',
}


def normalize_name(s):
    """名字归一化：去空格 + 转小写 + 常用繁体字归简（用于 LOL 名与玩家池匹配容错）"""
    s = (s or '').strip().lower()
    return ''.join(TS_MAP.get(ch, ch) for ch in s)


# ---------- 对局智能分析（比赛日 = 当天 18:00 ~ 次日 06:00；随对局自动重算，凌晨 3 点定版） ----------
def prev_day_str(mmdd):
    """前一天：'10-15' -> '10-14'"""
    m = re.match(r'^(\d{1,2})-(\d{1,2})$', mmdd or '')
    if not m:
        return mmdd
    d = datetime.date(2026, int(m.group(1)), int(m.group(2))) - datetime.timedelta(days=1)
    return '%02d-%02d' % (d.month, d.day)


def game_day_key(t):
    """对局时间的比赛日：18:00~23:59 归当天，次日凌晨 00:00~06:00 归前一晚（白天 06:00~18:00 也归前一晚）"""
    m = re.match(r'^(\d{2})-(\d{2}) (\d{2}):', t or '')
    if not m:
        return None
    h = int(m.group(3))
    if h >= 18:
        return m.group(1) + '-' + m.group(2)
    return prev_day_str(m.group(1) + '-' + m.group(2))


def _half_up(x, n=0):
    """四舍五入（与前端 JS Math.round/toFixed 一致的半进位）"""
    p = 10 ** n
    v = math.floor(abs(x) * p + 0.5) / p
    return -v if x < 0 else v


def build_day_analytics(gs):
    """按一个比赛日的已结算对局列表计算四榜单（战力 v4 公式）。
    v4 满配：KDA×40% + 伤害×15% + 承伤×15% + 治疗×15% + 经济×15%，×胜率；
    数据维度不齐时降级：v4→v3(无治疗)→v2(无承伤)→v1(纯 KDA×胜率)。
    返回 {'games': 局数, 'rows': [{tag, text}×4]}"""
    pstat, name_of = {}, {}
    for h in gs:
        teams = (h['teamA'], h['teamB'])
        dmg_sum = [0, 0]
        gold_sum = [0, 0]
        tk_sum = [0, 0]
        heal_sum = [0, 0]
        for i in (0, 1):
            for p in teams[i]:
                if 'dmg' in p:
                    dmg_sum[i] += p['dmg']
                    gold_sum[i] += p['gold']
                if 'tk' in p:
                    tk_sum[i] += p['tk']
                if 'heal' in p:
                    heal_sum[i] += p['heal']
        for i in (0, 1):
            is_win = (h['result'] == 'A' and i == 0) or (h['result'] == 'B' and i == 1)
            n = len(teams[i])
            for p in teams[i]:
                st = pstat.setdefault(p['id'], {'name': p['name'], 'k': 0, 'd': 0, 'a': 0, 'w': 0, 't': 0,
                                               'kg': 0, 'dF': 0.0, 'gF': 0.0, 'tF': 0.0, 'hF': 0.0,
                                               'eg': 0, 'tg': 0, 'hg': 0})
                name_of[p['id']] = p['name']
                st['t'] += 1
                if is_win:
                    st['w'] += 1
                if 'kills' in p:          # 仅 LOL 局有 KDA
                    st['kg'] += 1
                    st['k'] += p['kills']
                    st['d'] += p['deaths']
                    st['a'] += p['assists']
                    if 'dmg' in p and dmg_sum[i] > 0:    # 队内伤害/经济倍率
                        st['eg'] += 1
                        st['dF'] += p['dmg'] / dmg_sum[i] * n
                        st['gF'] += p['gold'] / gold_sum[i] * n
                    if 'tk' in p and tk_sum[i] > 0:      # 承伤倍率（坦克前排贡献）
                        st['tg'] += 1
                        st['tF'] += p['tk'] / tk_sum[i] * n
                    if 'heal' in p and heal_sum[i] > 0:    # 治疗倍率（辅助/奶妈贡献）
                        st['hg'] += 1
                        st['hF'] += p['heal'] / heal_sum[i] * n
    best = worst = None
    for st in pstat.values():       # dict 保持插入序，平局裁决与前端一致
        if not st['kg']:
            continue
        rate = st['w'] / st['t']
        kda = (st['k'] + 0.4 * st['a']) / max(st['d'], 1)
        if st['hg'] > 0 and st['tg'] > 0 and st['eg'] > 0:   # v4：五维满配（含治疗）
            idx = (0.4 * kda + 0.15 * (st['dF'] / st['eg']) + 0.15 * (st['tF'] / st['tg'])
                   + 0.15 * (st['hF'] / st['hg']) + 0.15 * (st['gF'] / st['eg'])) * rate
        elif st['tg'] > 0 and st['eg'] > 0:                  # v3 降级（无治疗）：KDA×40% + 伤害×25% + 承伤×20% + 经济×15%
            idx = (0.4 * kda + 0.25 * (st['dF'] / st['eg']) + 0.2 * (st['tF'] / st['tg'])
                   + 0.15 * (st['gF'] / st['eg'])) * rate
        elif st['eg'] > 0:                                    # v2 降级（无承伤无治疗）
            idx = (0.5 * kda + 0.25 * (st['dF'] / st['eg']) + 0.25 * (st['gF'] / st['eg'])) * rate
        else:                                                # v1 降级：纯 KDA×胜率
            idx = kda * rate
        st['rate'] = rate
        st['idx'] = idx
        if best is None or idx > best['idx']:
            best = st
        if worst is None or idx < worst['idx']:
            worst = st
    pairs = {}
    for h in gs:
        teams = (h['teamA'], h['teamB'])
        for i in (0, 1):
            is_win = (h['result'] == 'A' and i == 0) or (h['result'] == 'B' and i == 1)
            ids = sorted(str(p['id']) for p in teams[i])   # 字符串排序，与前端一致
            for a_i in range(len(ids)):
                for b_i in range(a_i + 1, len(ids)):
                    key = ids[a_i] + '|' + ids[b_i]
                    ps = pairs.setdefault(key, {'i1': ids[a_i], 'i2': ids[b_i], 'w': 0, 't': 0})
                    ps['t'] += 1
                    if is_win:
                        ps['w'] += 1
    best_pair = worst_pair = None
    for ps in pairs.values():
        if ps['t'] < 2:                        # 同队≥2局才参评
            continue
        ps['rate'] = ps['w'] / ps['t']
        if best_pair is None or ps['rate'] > best_pair['rate'] or (ps['rate'] == best_pair['rate'] and ps['t'] > best_pair['t']):
            best_pair = ps
        if worst_pair is None or ps['rate'] < worst_pair['rate'] or (ps['rate'] == worst_pair['rate'] and ps['t'] > worst_pair['t']):
            worst_pair = ps
    rows = [
        {'tag': 'bestPair', 'text': ('%s + %s（同队 %d 局 · 胜率 %d%%）'
                                     % (name_of[best_pair['i1']], name_of[best_pair['i2']], best_pair['t'], _half_up(best_pair['rate'] * 100)))
                                    if best_pair else '数据不足（需同队≥2局的组合）'},
        {'tag': 'best', 'text': ('%s（战力 %.1f · %d 局胜率 %d%%）'
                                 % (best['name'], _half_up(best['idx'], 1), best['t'], _half_up(best['rate'] * 100)))
                                if best else '本日无 KDA 数据'},
        {'tag': 'worst', 'text': ('%s（战力 %.1f · %d 局胜率 %d%%）'
                                  % (worst['name'], _half_up(worst['idx'], 1), worst['t'], _half_up(worst['rate'] * 100)))
                                 if worst else '本日无 KDA 数据'},
        {'tag': 'worstPair', 'text': ('%s + %s（同队 %d 局 · 胜率 %d%%）'
                                      % (name_of[worst_pair['i1']], name_of[worst_pair['i2']], worst_pair['t'], _half_up(worst_pair['rate'] * 100)))
                                     if worst_pair else '数据不足（需同队≥2局的组合）'},
    ]
    return {'games': len(gs), 'rows': rows}


def regen_day(day):
    """重算某比赛日的智能分析并存档；该日已无已结算对局则删档"""
    if not day:
        return
    conn = get_db()
    gs = []
    for r in conn.execute("SELECT time, team_a, team_b, result FROM games WHERE result IS NOT NULL ORDER BY id").fetchall():
        if game_day_key(r['time']) == day:
            gs.append({'teamA': json.loads(r['team_a']), 'teamB': json.loads(r['team_b']), 'result': r['result']})
    if not gs:
        conn.execute('DELETE FROM analytics WHERE day=?', (day,))
        conn.commit()
        conn.close()
        return
    data = build_day_analytics(gs)
    conn.execute('INSERT OR REPLACE INTO analytics(day,games_count,content,generated_at) VALUES(?,?,?,?)',
                 (day, data['games'], json.dumps(data['rows'], ensure_ascii=False), time.strftime('%m-%d %H:%M')))
    conn.commit()
    conn.close()


def regen_days_of(games_rows):
    """按对局记录批量重算涉及的比赛日"""
    days = set()
    for r in games_rows:
        k = game_day_key(r['time'] if isinstance(r, dict) is False else r.get('time'))
        if k:
            days.add(k)
    for d in sorted(days):
        regen_day(d)


def backfill_analytics():
    """启动时补档：为所有已结算对局涉及、但尚无存档的比赛日生成分析"""
    conn = get_db()
    days = set()
    for r in conn.execute("SELECT time FROM games WHERE result IS NOT NULL").fetchall():
        k = game_day_key(r['time'])
        if k:
            days.add(k)
    have = {r['day'] for r in conn.execute('SELECT day FROM analytics').fetchall()}
    conn.close()
    for d in sorted(days - have):
        regen_day(d)


def _analytics_scheduler():
    """每晚凌晨 3 点自动定版前一晚（当前比赛日）的智能分析；服务重启不影响，到点自动补跑"""
    last_done = None
    while True:
        try:
            lt = time.localtime()
            today = time.strftime('%m-%d', lt)
            if lt.tm_hour == 3 and last_done != today:
                day = prev_day_str(today)   # 凌晨 3 点 <18 点（且 <06 点）→ 当前比赛日 = 昨晚
                regen_day(day)
                last_done = today
                print('[智能分析] 凌晨定版完成: %s 晚' % day)
        except Exception as e:
            print('[智能分析] 调度异常: %r' % e)
        time.sleep(600)


@app.route('/api/analytics')
def api_analytics():
    """已存档的智能分析（按比赛日倒序）；缺档的比赛日由写入路径/凌晨调度自动补齐"""
    conn = get_db()
    out = []
    for r in conn.execute('SELECT * FROM analytics ORDER BY day DESC').fetchall():
        out.append({'day': r['day'], 'games': r['games_count'],
                    'rows': json.loads(r['content']), 'generatedAt': r['generated_at']})
    conn.close()
    return jsonify({'ok': True, 'days': out})


# ---------- 本机 LOL 同步按钮 ----------
def is_local_lol_sync_request():
    local_hosts = {'localhost', '127.0.0.1', '::1'}
    host = (urlsplit('//' + request.host).hostname or '').lower()
    if request.remote_addr not in ('127.0.0.1', '::1') or host not in local_hosts:
        return False
    origin = request.headers.get('Origin') or request.headers.get('Referer')
    return bool(origin and urlsplit(origin).hostname in local_hosts)


def run_local_lol_sync(mode):
    if not is_local_lol_sync_request():
        return jsonify({'ok': False, 'error': '此操作只能在运行 LOL 的电脑本机执行'}), 403
    script = os.path.join(BASE, 'lol_sync.ps1')
    if not os.path.isfile(script):
        return jsonify({'ok': False, 'error': '找不到 lol_sync.ps1，请确认同步脚本与 app.py 在同一文件夹'}), 404
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix='lol_sync_', suffix='.json', delete=False) as output:
            output_path = output.name
        proc = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script,
             '-Mode', mode, '-OutputFile', output_path],
            capture_output=True, text=True, timeout=240
        )
        if os.path.isfile(output_path):
            with open(output_path, 'r', encoding='utf-8-sig') as output:
                result = json.load(output)
            status = 200 if result.get('ok') else 400
            return jsonify(result), status
        details = (proc.stderr or proc.stdout or '').strip()
        return jsonify({'ok': False, 'error': details or 'LOL 同步脚本未返回结果'}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'error': '读取超时，请确认 LOL 客户端已登录并在客户端打开过战绩页后重试'}), 504
    except Exception as exc:
        return jsonify({'ok': False, 'error': '无法启动 LOL 同步脚本：' + str(exc)}), 500
    finally:
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass


@app.route('/api/lol-sync/preview', methods=['POST'])
def api_lol_sync_preview():
    response, status = run_local_lol_sync('Preview')
    if status != 200:
        return response, status
    result = response.get_json()
    conn = get_db()
    imported = {r['lol_game_id'] for r in conn.execute(
        'SELECT lol_game_id FROM games WHERE lol_game_id IS NOT NULL').fetchall()}
    names = [r['name'] for r in conn.execute('SELECT name FROM players').fetchall()]
    known_names = {normalize_name(name) for name in names}
    conn.close()
    for game in result.get('games', []):
        game['alreadyImported'] = game.get('gameId') in imported
        game['newPlayers'] = sorted({
            player.get('name', '').strip()
            for team in (game.get('teamA', []), game.get('teamB', []))
            for player in team
            if player.get('name', '').strip() and normalize_name(player['name']) not in known_names
        })
    return jsonify(result)


@app.route('/api/lol-sync/import', methods=['POST'])
def api_lol_sync_import():
    return run_local_lol_sync('Sync')


# ---------- LOL 战绩自动导入 ----------
@app.route('/api/lol/import', methods=['POST'])
def api_lol_import():
    """接收数据源电脑（运行 LOL 客户端那台）推送的自定义对局：
    自动建局 + 录入结果 + 计分（胜 +5 / 负 -5；四杀标记全队 ±1、五杀标记全队 ±2，可叠加）。
    按 gameId 幂等去重；名字匹配带繁简/大小写容错；按每队人数归入 3v3/4v4/5v5 榜。"""
    data = request.get_json(force=True)
    games = data.get('games') or []
    results = []
    imported = 0
    dirty_days = set()   # 本次写入影响的比赛日，结束后统一重算分析
    conn = get_db()
    pool_rows = {r['name']: r for r in conn.execute('SELECT * FROM players').fetchall()}
    pool_norm = {}
    for k, v in pool_rows.items():
        pool_norm.setdefault(normalize_name(k), v)

    def find_pr(name):
        """按名字找玩家池记录：精确 -> 归一化（繁简/大小写容错）"""
        if (name or '') in pool_rows:
            return pool_rows[name]
        return pool_norm.get(normalize_name(name))

    def ensure_pr(name):
        """LOL 自动导入遇到新名字时创建 0 分档案。"""
        pr = find_pr(name)
        if pr or not (name or '').strip():
            return pr
        clean_name = name.strip()
        pid = 'p_lol_' + uuid.uuid4().hex
        conn.execute('INSERT INTO players(id,name,score3,score4,score5,delta) VALUES(?,?,0,0,0,0)',
                     (pid, clean_name))
        pr = conn.execute('SELECT * FROM players WHERE id=?', (pid,)).fetchone()
        pool_rows[clean_name] = pr
        pool_norm.setdefault(normalize_name(clean_name), pr)
        return pr

    def snap(rows, col):
        out = []
        for p in rows:
            pr = find_pr(p.get('name'))
            item = {'id': pr['id'], 'name': pr['name'], 'score': pr[col]}
            # LOL 导入的局带 KDA（击杀/死亡/助攻）+ 伤害/承伤/经济，手动抽签的局无此数据
            if p.get('kills') is not None:
                item['kills'] = int(p.get('kills') or 0)
                item['deaths'] = int(p.get('deaths') or 0)
                item['assists'] = int(p.get('assists') or 0)
            if p.get('dmg') is not None:
                item['dmg'] = int(p.get('dmg') or 0)
                item['gold'] = int(p.get('gold') or 0)
            if p.get('tk') is not None:
                item['tk'] = int(p.get('tk') or 0)
            if p.get('heal') is not None:
                item['heal'] = int(p.get('heal') or 0)
            if p.get('champ') is not None:
                item['champ'] = str(p.get('champ') or '')
            out.append(item)
        return out

    for g in games:
        gid = g.get('gameId')
        # 幂等：同一 LOL 对局只导入一次；若已导入的局缺 KDA/伤害/经济/承伤而新数据带，则只补全这些字段（积分/胜负不动）
        if gid is not None and conn.execute('SELECT 1 FROM games WHERE lol_game_id=?', (gid,)).fetchone():
            old_row = conn.execute('SELECT time, team_a, team_b FROM games WHERE lol_game_id=?', (gid,)).fetchone()
            parts_pre = g.get('participants') or []
            byid = {}
            for p in parts_pre:
                pr = find_pr(p.get('name'))
                if not pr:
                    continue
                rec = {}
                if p.get('kills') is not None:
                    rec['kills'] = int(p.get('kills') or 0)
                    rec['deaths'] = int(p.get('deaths') or 0)
                    rec['assists'] = int(p.get('assists') or 0)
                if p.get('dmg') is not None:
                    rec['dmg'] = int(p.get('dmg') or 0)
                    rec['gold'] = int(p.get('gold') or 0)
                if p.get('tk') is not None:
                    rec['tk'] = int(p.get('tk') or 0)
                if p.get('heal') is not None:
                    rec['heal'] = int(p.get('heal') or 0)
                if p.get('champ') is not None:
                    rec['champ'] = str(p.get('champ') or '')
                if rec:
                    byid[pr['id']] = rec
            patched = 0
            if byid:
                ta = json.loads(old_row['team_a'])
                tb = json.loads(old_row['team_b'])
                for arr in (ta, tb):
                    for m in arr:
                        if m.get('id') not in byid:
                            continue
                        rec = byid[m['id']]
                        hit = False
                        if 'kills' not in m and 'kills' in rec:
                            m['kills'], m['deaths'], m['assists'] = rec['kills'], rec['deaths'], rec['assists']
                            hit = True
                        if 'dmg' not in m and 'dmg' in rec:
                            m['dmg'], m['gold'] = rec['dmg'], rec['gold']
                            hit = True
                        if 'tk' not in m and 'tk' in rec:
                            m['tk'] = rec['tk']
                            hit = True
                        if 'heal' not in m and 'heal' in rec:
                            m['heal'] = rec['heal']
                            hit = True
                        if 'champ' not in m and 'champ' in rec:
                            m['champ'] = rec['champ']
                            hit = True
                        if hit:
                            patched += 1
                if patched:
                    conn.execute('UPDATE games SET team_a=?, team_b=? WHERE lol_game_id=?',
                                 (json.dumps(ta, ensure_ascii=False), json.dumps(tb, ensure_ascii=False), gid))
                    conn.commit()
                    results.append({'gameId': gid, 'status': 'updated', 'reason': '战绩明细(英雄/伤害/承伤/治疗/经济等)已补全(%d人)' % patched})
                    _dk = game_day_key(old_row['time'])
                    if _dk:
                        dirty_days.add(_dk)
                    continue
            results.append({'gameId': gid, 'status': 'skipped', 'reason': '已导入过'})
            continue
        parts = g.get('participants') or []
        teams = {}
        for p in parts:
            t = int(p.get('team') or 0)
            teams.setdefault(t, []).append(p)
        if len(teams) != 2:
            results.append({'gameId': gid, 'status': 'skipped', 'reason': '队伍数量异常（应 2 队）'})
            continue
        ks = sorted(teams.keys())
        n1, n2 = len(teams[ks[0]]), len(teams[ks[1]])
        if n1 != n2 or not (3 <= n1 <= 5):
            results.append({'gameId': gid, 'status': 'skipped',
                            'reason': '两队人数需相同且 3-5 人（当前 %d vs %d）' % (n1, n2)})
            continue
        n = n1
        # 胜负：恰好一队全员胜
        win_teams = set(int(p.get('team') or 0) for p in parts if p.get('win'))
        if len(win_teams) != 1 or win_teams.pop() not in teams:
            results.append({'gameId': gid, 'status': 'skipped', 'reason': '胜负数据异常'})
            continue
        win_teams = set(int(p.get('team') or 0) for p in parts if p.get('win'))
        wt = win_teams.pop()
        missing_names = [str(p.get('name') or '?') for p in parts if not find_pr(p.get('name'))]
        if any(not (p.get('name') or '').strip() for p in parts):
            results.append({'gameId': gid, 'status': 'skipped', 'reason': '玩家名称缺失，无法自动建档'})
            continue
        for name in missing_names:
            ensure_pr(name)
        # 分队：标准 teamId=100 蓝方 → A 队、200 红方 → B 队；非标准键按顺序
        if 100 in teams and 200 in teams:
            team_a, team_b = teams[100], teams[200]
        else:
            team_a, team_b = teams[ks[0]], teams[ks[1]]
        result = 'A' if wt == (100 if 100 in teams else ks[0]) else 'B'
        col = score_col(n)
        # 四杀/五杀标记：胜方成员（4v4 仅四杀；5v5 四杀五杀均可），同人多次 → id 重复出现 N 次
        quadra_list, penta_list = [], []
        if n in (4, 5):
            for p in teams[wt]:
                mk = p.get('multiKills') or {}
                q = int(mk.get('quadra') or 0)          # 四杀：4v4 / 5v5 都计
                p5 = int(mk.get('penta') or 0) if n == 5 else 0   # 五杀：仅 5v5
                if q > 0:
                    quadra_list.extend([find_pr(p['name'])['id']] * q)
                if p5 > 0:
                    penta_list.extend([find_pr(p['name'])['id']] * p5)
        n_kill = len(quadra_list) + len(penta_list)
        ts = time.strftime('%m-%d %H:%M', time.localtime((g.get('gameCreation') or 0) / 1000.0))
        conn.execute('INSERT INTO games(time,team_a,team_b,result,draw_type,kill_ids,penta_ids,lol_game_id) VALUES(?,?,?,?,?,?,?,?)',
                     (ts, json.dumps(snap(team_a, col), ensure_ascii=False),
                       json.dumps(snap(team_b, col), ensure_ascii=False),
                       result, 'lol', json.dumps(quadra_list), json.dumps(penta_list), gid))
        # 计分（与手动录入同规则：胜 +5+加成 / 负 -5-加成；四杀×1 + 五杀×2）
        win_ids = {x['id'] for x in snap(team_a if result == 'A' else team_b, col)}
        lose_ids = {x['id'] for x in snap(team_b if result == 'A' else team_a, col)}
        bonus_total = len(quadra_list) * KILL_BONUS_QUADRA + len(penta_list) * KILL_BONUS_PENTA
        for pr in conn.execute('SELECT * FROM players').fetchall():
            delta = 0
            if pr['id'] in win_ids:
                delta = WIN_SCORE + bonus_total
            elif pr['id'] in lose_ids:
                delta = -LOSE_SCORE - bonus_total
            if delta:
                conn.execute('UPDATE players SET %s=%s+?, delta=? WHERE id=?' % (col, col), (delta, delta, pr['id']))
        imported += 1
        dirty_days.add(game_day_key(ts))
        results.append({'gameId': gid, 'status': 'imported', 'result': result, 'kills': n_kill})
    conn.commit()
    conn.close()
    for d in sorted(dirty_days):          # 导入/补全后自动重算受影响比赛日的智能分析（叠加更新）
        if d:
            regen_day(d)
    return jsonify({'ok': True, 'imported': imported, 'results': results})


# ---------- 其他 ----------
@app.route('/api/public-url')
def api_public_url():
    """对外分享地址：花生壳固定域名优先（永久不变）；未配置时回退 cpolar 临时隧道地址。"""
    FIXED_URL = 'https://124777lk2jf53.vicp.fun'   # 花生壳固定域名（免费壳域名，映射 127.0.0.1:9529）
    import re
    import urllib.request
    # 固定域名探活：通了就用它（花生壳客户端在线时恒通）
    try:
        urllib.request.urlopen(urllib.request.Request(FIXED_URL, method='HEAD'), timeout=4)
        return jsonify({'ok': True, 'url': FIXED_URL})
    except Exception:
        pass
    pages = ('http://127.0.0.1:9203/http/in', 'http://127.0.0.1:9201/http/in',
             'http://127.0.0.1:9201/', 'http://127.0.0.1:4040/http/in')
    for url in pages:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            html = urllib.request.urlopen(req, timeout=3).read().decode('utf-8', 'ignore')
            # 首选：隧道列表里 LocalAddr 指向 9529 的公网地址（https 优先）
            pairs = re.findall(r'"PublicUrl":"(https?://[a-z0-9]+\.r\d+\.cpolar\.(?:top|cn))".*?"LocalAddr":"([^"]+)"', html)
            if pairs:
                hits = [u for u, addr in pairs if '9529' in addr]
                if hits:
                    https_hits = [u for u in hits if u.startswith('https://')]
                    return jsonify({'ok': True, 'url': https_hits[0] if https_hits else hits[0]})
            # 兜底：页面直接正则
            for proto in ('https', 'http'):
                m = re.search(proto + r'://[a-z0-9]+\.r\d+\.cpolar\.(?:top|cn)', html)
                if m:
                    return jsonify({'ok': True, 'url': m.group(0)})
        except Exception:
            continue
    return jsonify({'ok': False, 'url': None})


@app.route('/api/clear-scores', methods=['POST'])
def api_clear_scores():
    conn = get_db()
    conn.execute('UPDATE players SET score3=0, score4=0, score5=0, delta=0')
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/import', methods=['POST'])
def api_import():
    data = request.get_json(force=True)
    items = data.get('players', [])
    conn = get_db()
    cur = conn.execute('SELECT COUNT(*) AS c FROM players')
    if cur.fetchone()['c'] > 0:
        conn.close()
        return jsonify({'ok': False, 'msg': '玩家池非空，无法导入'})
    for i, it in enumerate(items):
        name = (it.get('name') or '').strip()
        score = int(it.get('score') or 0)
        if name:
            conn.execute('INSERT INTO players(id,name,score3,score4,score5,delta) VALUES(?,?,0,0,?,0)',
                         ('p_imp_%d' % i, name, score))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


if __name__ == '__main__':
    init_db()
    backfill_analytics()   # 启动补档：历史对局中缺存档的比赛日自动生成
    threading.Thread(target=_analytics_scheduler, daemon=True).start()
    print('智能分析调度线程已启动（每晚 03:00 自动定版前一晚）')
    print('5v5 均衡抽签服务已启动: http://127.0.0.1:%d' % PORT)
    app.run(host='0.0.0.0', port=PORT, debug=False)