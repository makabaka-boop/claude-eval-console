"""Drive a short, requirement-focused browser demonstration through CDP.

The browser is an isolated Chrome process started only for one recording.  CDP
lets us find controls by their visible/accessibility names, while moving the
real pointer makes the recording understandable without exposing the desktop.
"""
from __future__ import annotations

import ctypes
import base64
import binascii
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from . import native_picker


UNSAFE_WORDS = (
    "delete", "remove", "logout", "sign out", "drop", "erase", "purchase",
    "pay", "删除", "移除", "退出", "清空", "支付", "购买",
)

# Long IDs/slugs are slow and error-prone when replayed one character at a
# time during a recording.  Treat a single token of this size like the other
# technical payloads and insert it in one paste-style operation.
LONG_TOKEN_PASTE_THRESHOLD = 20
LONG_TEXT_PASTE_THRESHOLD = 40
MAX_ACTIONS = 11

# Captions and domain terms are commonly copied from an approved script,
# glossary, rundown, or asset label. Replaying them character by character is
# both less realistic and more likely to introduce spelling mistakes. Match
# field/plan context instead of guessing from the value alone so ordinary
# short names and prose can keep the paced typing path.
PASTE_CONTEXT_RE = re.compile(
    r"粘贴|复制|剪贴板|字幕|唱词|台词|原文|译文|专业名词|专业术语|术语|专有名词|专名|"
    r"人名|地名|机构名|作品名|曲目|型号|标准号|"
    r"\b(?:paste|clipboard|caption|subtitle|transcript|source text|translation|technical term|proper noun)\b",
    re.IGNORECASE,
)
PASTE_FOCUS_PAUSE = (0.45, 0.8)
PASTE_SETTLE_PAUSE = (0.5, 0.85)

DEFAULT_CADENCE_CONFIG = {
    "tempo": [0.92, 1.12],
    "reading_bases": {"fill": 0.75, "select": 0.95, "upload": 0.9, "click": 1.25},
    "result_extra": [1.15, 1.7],
    "opening": [1.1, 1.7],
    "browser_focus": [0.35, 0.55],
    "upload_opening": [0.14, 0.28],
    "upload_browser_focus": [0.06, 0.12],
    "picker_open": [0.52, 0.82],
    "picker_retry": [0.45, 0.72],
    "picker_shortcut": [0.42, 0.72],
    "picker_path": [0.42, 0.72],
    "picker_folder": [0.24, 0.42],
    "picker_select": [0.18, 0.32],
    "picker_open_button": [0.18, 0.32],
    "picker_select_move": [0.55, 0.9],
    "picker_open_button_move": [0.48, 0.8],
    "upload_settle": [0.65, 1.05],
}


def _cadence_config_path():
    configured = os.environ.get("PAIRWISE_RECORDING_CADENCE_FILE", "").strip()
    return (Path(configured).expanduser() if configured else
            Path(__file__).resolve().parent.parent / "config" / "recording-cadence.json")


def _cadence_config():
    """Read cadence settings for each recording so timing edits need no restart."""
    path = _cadence_config_path()
    try:
        custom = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        custom = {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"录屏节奏配置无法读取：{exc}") from exc
    if not isinstance(custom, dict):
        raise RuntimeError("录屏节奏配置必须是 JSON 对象")
    config = {**DEFAULT_CADENCE_CONFIG, **custom}
    bases = config.get("reading_bases")
    if not isinstance(bases, dict) or any(
            not isinstance(bases.get(name), (int, float)) or not 0.05 <= float(bases[name]) <= 3
            for name in ("fill", "select", "upload", "click")):
        raise RuntimeError("录屏节奏 reading_bases 必须包含合理的 fill/select/upload/click 秒数")
    for key, value in config.items():
        if key == "reading_bases":
            continue
        if (not isinstance(value, list) or len(value) != 2
                or any(not isinstance(item, (int, float)) for item in value)
                or not 0.04 <= float(value[0]) <= float(value[1]) <= 3):
            raise RuntimeError(f"录屏节奏 {key} 必须是 0.04 至 3 秒的递增区间")
    return config


def _native_picker_permission_hint() -> str:
    executable = Path(sys.executable)
    return (
        f"录屏服务使用 {executable}；请在系统设置→隐私与安全性→辅助功能中"
        f"打开“{executable.name}”这一项"
    )


def _page_matches(item: dict, expected_url=None) -> bool:
    if item.get("type") != "page" or not item.get("webSocketDebuggerUrl"):
        return False
    actual = urlsplit(str(item.get("url") or ""))
    if expected_url:
        wanted = urlsplit(expected_url)
        return actual.scheme == wanted.scheme and actual.netloc == wanted.netloc
    return actual.scheme in {"http", "https", "file", "data"}


class CDP:
    def __init__(self, port: int, expected_url=None):
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - installed on the mac host
            raise RuntimeError("缺少浏览器控制组件 websocket-client") from exc
        deadline = time.monotonic() + 25
        target = None
        last_error = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                    targets = json.load(response)
                target = next((item for item in targets if _page_matches(item, expected_url)), None)
                if target:
                    break
            except Exception as exc:  # browser can take a moment to expose CDP
                last_error = exc
            time.sleep(0.3)
        if not target:
            raise RuntimeError(f"浏览器调试端口没有就绪：{last_error or '找不到页面'}")
        # Current Chrome rejects websocket-client's synthetic localhost Origin.
        # Omitting it is sufficient for this loopback-only, isolated profile
        # and avoids starting Chrome with a broad remote-allow-origins flag.
        self.socket = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=60, suppress_origin=True
        )
        self.sequence = 0

    def close(self):
        try:
            self.socket.close()
        except Exception:
            pass

    def call(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        self.socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            payload = json.loads(self.socket.recv())
            if payload.get("id") != request_id:
                continue
            if payload.get("error"):
                raise RuntimeError(payload["error"].get("message") or method)
            return payload.get("result") or {}

    def evaluate(self, expression):
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return (result.get("result") or {}).get("value")


def _same_origin(actual_url, expected_url):
    actual, expected = urlsplit(str(actual_url or "")), urlsplit(expected_url)
    return actual.scheme == expected.scheme and actual.netloc == expected.netloc


def _wait_for_page(cdp: CDP, deadline=None, settle=True, expected_url=None):
    started = time.monotonic()
    end = min(started + 60, deadline) if deadline is not None else started + 60
    navigated = False
    while time.monotonic() < end:
        if expected_url:
            current_url = cdp.evaluate("location.href")
            if not _same_origin(current_url, expected_url):
                # Chrome can advertise the app URL in /json while its default
                # execution context is still about:blank. readyState is already
                # "complete" there, so it cannot be used as the sole gate.
                if (not navigated and current_url in {"", "about:blank"}
                        and time.monotonic() - started >= 1):
                    cdp.call("Page.navigate", {"url": expected_url})
                    navigated = True
                time.sleep(0.25)
                continue
        state = cdp.evaluate("document.readyState")
        if state in {"interactive", "complete"}:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("等待演示页面就绪超时，已保留现场")
    cdp.call("Page.bringToFront")
    if settle:
        time.sleep(1.2)


def viewport_rect(cdp: CDP) -> str:
    """Return the web-content rectangle, excluding Chrome tabs and toolbars."""
    dimensions = cdp.evaluate("""({
      x: window.screenX, y: window.screenY,
      outerWidth: window.outerWidth, outerHeight: window.outerHeight,
      innerWidth: window.innerWidth, innerHeight: window.innerHeight
    })""") or {}
    outer_width = int(dimensions.get("outerWidth") or 0)
    outer_height = int(dimensions.get("outerHeight") or 0)
    inner_width = int(dimensions.get("innerWidth") or 0)
    inner_height = int(dimensions.get("innerHeight") or 0)
    if min(outer_width, outer_height, inner_width, inner_height) <= 0:
        raise RuntimeError("无法取得浏览器页面尺寸")
    side = max(0, (outer_width - inner_width) // 2)
    bottom = side
    left = int(dimensions.get("x") or 0) + side
    top = int(dimensions.get("y") or 0) + max(0, outer_height - inner_height - bottom)
    return f"{left},{top},{inner_width},{inner_height}"


def _resolve_script(target: str, action_type: str) -> str:
    return """(async () => {
      const wanted = %s;
      const actionType = %s;
      const norm = value => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const deepAll = (root, selector) => {
        const matches = [...root.querySelectorAll(selector)];
        for (const host of root.querySelectorAll('*')) {
          if (host.shadowRoot) matches.push(...deepAll(host.shadowRoot, selector));
        }
        return matches;
      };
      const visible = el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 4 && r.height > 4 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const gridAddress = String(wanted).match(/^R(\\d+)\\s+C(\\d+)$/i);
      if (gridAddress && (actionType === 'click' || actionType === 'fill')) {
        const selector = '[data-cell][data-r="' + gridAddress[1] + '"][data-c="' + gridAddress[2] + '"]';
        const alternate = '[data-cell="' + gridAddress[1] + ':' + gridAddress[2] + '"]';
        const cell = document.querySelector(selector) || document.querySelector(alternate);
        if (!cell || !visible(cell)) return null;
        const r = cell.getBoundingClientRect();
        const x = r.left + r.width / 2, y = r.top + r.height / 2;
        const hit = document.elementFromPoint(x, y);
        if (!hit || (hit !== cell && !cell.contains(hit))) return null;
        return {x, y, width:r.width, height:r.height, tag:cell.tagName,
          name:'R' + gridAddress[1] + ' C' + gridAddress[2], gridCell:true};
      }
      const projection = actionType === 'click' && String(wanted).match(/(XY|XZ|YZ)\\s*投影/i);
      const requestedAxes = [...String(wanted).matchAll(/([xyz])\\s*=\\s*(\\d+)/gi)];
      if (projection && requestedAxes.length >= 2) {
        const plane = projection[1].toUpperCase();
        const indices = Object.fromEntries(requestedAxes.map(match => [match[1].toUpperCase(), Number(match[2])]));
        const figure = [...document.querySelectorAll('figure')].find(el =>
          String(el.querySelector('figcaption')?.textContent || '').toUpperCase().includes(plane + ' 投影'));
        if (!figure) return null;
        figure.scrollIntoView({behavior: 'auto', block: 'center'});
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const cells = [...figure.querySelectorAll('svg g.cell')].filter(visible);
        const point = el => {
          const r = (el.querySelector('rect') || el).getBoundingClientRect();
          return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
        };
        const xs = [...new Set(cells.map(el => point(el).x))].sort((a, b) => a - b);
        const ys = [...new Set(cells.map(el => point(el).y))].sort((a, b) => b - a);
        const x = xs[indices[plane[0]]], y = ys[indices[plane[1]]];
        const winner = cells.find(el => { const p = point(el); return p.x === x && p.y === y; });
        const expected = String(wanted).match(/显示\\s*([01])/);
        if (!winner || (expected && winner.querySelector('text')?.textContent?.trim() !== expected[1])) return null;
        const hit = document.elementFromPoint(x, y);
        if (!hit || (hit !== winner && !winner.contains(hit))) return null;
        const r = (winner.querySelector('rect') || winner).getBoundingClientRect();
        return {x, y, width: r.width, height: r.height, tag: winner.tagName,
          name: String(figure.querySelector('figcaption')?.textContent || '').trim()};
      }
      const label = el => {
        const linked = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
        const parent = el.closest('label');
        const previous = el.previousElementSibling && el.previousElementSibling.matches('label')
          ? el.previousElementSibling : null;
        const row = el.closest('tr');
        const panel = el.closest('.panel');
        const section = el.closest('section');
        const ariaGroup = el.closest('[aria-label]');
        const parts = [el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
          el.querySelector?.('title')?.textContent,
          el.getAttribute('placeholder'), el.getAttribute('name'), linked && linked.innerText,
          parent && parent.innerText, previous && previous.innerText, row && row.innerText,
          panel?.querySelector?.('h2,h3')?.textContent,
          section?.querySelector?.('h2')?.textContent,
          ariaGroup && ariaGroup !== el && typeof ariaGroup.getAttribute === 'function'
            && ariaGroup.getAttribute('aria-label'),
          String(el.getAttribute('type') || '').toLowerCase()==='checkbox' && 'checkbox 复选框 勾选'];
        return [...new Set(parts.filter(Boolean).map(value => String(value).trim()))].join(' ');
      };
      let selector = actionType === 'drag' ? 'canvas,.stage:has(img),img[alt="original"]' :
        'button,[role="button"],a[href],[role="tab"],summary,input,textarea,select,[contenteditable="true"],svg circle:has(> title)';
      if (actionType === 'click') selector += ',tr,canvas';
      let items = deepAll(document, selector).filter(visible)
        .filter(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true');
      if (actionType === 'fill') items = items.filter(el => el.matches('input:not([type="file"]),textarea,[contenteditable="true"]'));
      if (actionType === 'select') items = items.filter(el => el.matches('select,[role="combobox"]'));
      const needle = norm(wanted);
      const anyCharacterOption = /字符选项.*任一|任一.*字符(?:选项|按钮)/.test(needle);
      if (anyCharacterOption) {
        const group = [...document.querySelectorAll('[aria-label]')]
          .find(el => norm(el.getAttribute('aria-label')) === '字符选项');
        const options = group ? [...group.querySelectorAll('button,[role="button"]')]
          .filter(visible).filter(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true') : [];
        if (options.length) items = options;
      }
      const quotedTerms = [...String(wanted).matchAll(/[“「"]([^”」"]+)[”」"]/g)]
        .map(match => norm(match[1])).filter(Boolean);
      const rowOrdinal = String(wanted).match(/第\\s*(\\d+)\\s*行/);
      const wantedRegion = actionType === 'fill'
        ? (/航路|route/.test(needle) ? '航路' : /禁区|zone/.test(needle) ? '禁区' : '') : '';
      const checkboxWanted = /checkbox|复选框|勾选/.test(needle);
      const wantedSide = /主输入|主路|primary/.test(needle) ? 'primary'
        : /备输入|备路|备用|backup/.test(needle) ? 'backup' : '';
      const rank = (el, index, term) => {
        const name = norm(label(el));
        let score = name === term ? 100 : name.startsWith(term) ? 70 : name.includes(term) ? 55
          : term.includes(name) && name.length > 1 ? 35 : 0;
        const quotedHits = quotedTerms.filter(value => name.includes(value)).length;
        if (quotedTerms.length && quotedHits)
          score = Math.max(score, 18 + quotedHits * 20);
        if (score > 0 && wantedRegion) {
          const heading = norm(el.closest('section')?.querySelector('h2,h3')?.textContent);
          if (!heading.includes(wantedRegion)) score = 0;
        }
        if (score > 0 && rowOrdinal && actionType === 'fill') {
          const rowIndex = norm(el.closest('tr')?.querySelector('td')?.textContent);
          if (rowIndex !== rowOrdinal[1]) score = 0;
        }
        if (score > 0 && checkboxWanted
            && String(el.getAttribute('type') || '').toLowerCase()==='checkbox') score += 15;
        if (score > 0 && wantedSide) {
          const region = el.closest('[data-testid^="card-"],.channel-card,.card');
          const identity = norm([el.getAttribute('data-testid'), region?.getAttribute('data-testid'),
            region?.innerText].filter(Boolean).join(' '));
          const sameSide = wantedSide === 'primary'
            ? /primary|主输入|主路/.test(identity)
            : /backup|备输入|备路|备用/.test(identity);
          const otherSide = wantedSide === 'primary'
            ? /backup|备输入|备路|备用/.test(identity)
            : /primary|主输入|主路/.test(identity);
          if (sameSide) score += 35;
          else if (otherSide) score = Math.max(1, score - 35);
        }
        if (!term || ((actionType === 'drag' || (actionType === 'click' && el.tagName === 'CANVAS'))
            && /canvas|画布|预览|裁切/.test(term))) score = Math.max(score, 1);
        if (score > 0 && el.matches('button,[role="button"]')) score += 3;
        return {el, name, score, index};
      };
      const sort = values => values.filter(item => item.score > 0)
        .sort((a, b) => b.score - a.score || a.index - b.index);
      let scored = sort(items.map((el, index) => rank(el, index, anyCharacterOption ? '' : needle)));
      const ordinal = String(wanted).match(/#\\s*(\\d+)/);
      const quoted = String(wanted).match(/[“「"]([^”」"]+)[”」"]/);
      const alias = quoted && norm(quoted[1]);
      let aliasUsed = false;
      if ((actionType === 'fill' || actionType === 'select') && alias && alias !== needle
          && (ordinal || !scored.length)) {
        const aliasMatches = sort(items.map((el, index) => rank(el, index, alias)));
        if (aliasMatches.length) {
          scored = aliasMatches;
          aliasUsed = true;
        }
      }
      let winner = scored[0];
      if (aliasUsed && ordinal && Number(ordinal[1]) >= scored.length) return null;
      if (aliasUsed && ordinal)
        winner = scored[Number(ordinal[1])];
      if (!winner) return null;
      const before = winner.el.getBoundingClientRect();
      if (before.top < 12 || before.bottom > innerHeight - 12 || before.left < 12 || before.right > innerWidth - 12) {
        winner.el.scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'nearest'});
        let last = before, stable = 0;
        for (let frame = 0; frame < 40; frame++) {
          await new Promise(resolve => requestAnimationFrame(resolve));
          const next = winner.el.getBoundingClientRect();
          stable = Math.abs(next.top-last.top)+Math.abs(next.left-last.left) < 0.5 ? stable+1 : 0;
          last = next;
          if (frame >= 5 && stable >= 3) break;
        }
      }
      const r = winner.el.getBoundingClientRect();
      const x = Math.max(4, Math.min(innerWidth-4, r.left+r.width/2));
      const y = Math.max(4, Math.min(innerHeight-4, r.top+r.height/2));
      const root = winner.el.getRootNode?.() || document;
      const hit = (root.elementFromPoint?.(x, y) || document.elementFromPoint(x, y));
      if (!hit || (hit !== winner.el && !winner.el.contains(hit))) return null;
      return {x: r.left + r.width / 2, y: r.top + r.height / 2,
        width: r.width, height: r.height, tag: winner.el.tagName, name: winner.name};
    })()""" % (json.dumps(target, ensure_ascii=False), json.dumps(action_type))


def _upload_target_script(target: str) -> str:
    """Locate a visible file control and its real (possibly hidden) input."""
    return """(async () => {
      const wanted = %s;
      const norm = value => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
      const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 4 && r.height > 4 && s.display !== 'none' && s.visibility !== 'hidden';
      };
      const candidateVisible = el => {
        if (!visible(el)) return false;
        if (!el.matches('input[type="file"]')) return true;
        const r = el.getBoundingClientRect();
        return r.right > 0 && r.left < innerWidth && r.bottom > 0 && r.top < innerHeight;
      };
      const inputs = [...document.querySelectorAll('input[type="file"]')];
      if (!inputs.length) return null;
      const label = el => {
        const linked = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
        const parent = el.closest('label');
        return [el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
          el.getAttribute('placeholder'), linked && linked.innerText, parent && parent.innerText]
          .filter(Boolean).join(' ');
      };
      const inputFor = el => {
        if (el.matches('input[type="file"]')) return el;
        if (el.matches('label')) {
          const linked = el.htmlFor && document.getElementById(el.htmlFor);
          if (linked && inputs.includes(linked)) return linked;
          const child = el.querySelector('input[type="file"]');
          if (child) return child;
        }
        const nearby = el.parentElement && [...el.parentElement.querySelectorAll('input[type="file"]')];
        if (nearby && nearby.length === 1) return nearby[0];
        if (nearby && nearby.length > 1 && el.matches('button,[role="button"]')) {
          const buttons = [...el.parentElement.querySelectorAll('button,[role="button"]')];
          const position = buttons.indexOf(el);
          if (buttons.length === nearby.length && position >= 0) return nearby[position];
        }
        return inputs.length === 1 && /file|upload|import|json|csv|choose|选择|上传|导入|文件/i.test(label(el))
          ? inputs[0] : null;
      };
      // Prefer the visible proxy before the file input itself.  A common
      // accessible pattern keeps the input far off screen (rather than with
      // display:none) inside a visible label.  Considering inputs first gave
      // both nodes the same name and selected the off-screen input.
      const candidates = [...document.querySelectorAll('label,button,[role="button"]'), ...inputs]
        .filter(el => candidateVisible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
        .map(el => ({el, input:inputFor(el)})).filter(item => item.input);
      const quoted = String(wanted).match(/[“「"]([^”」"]+)[”」"]/);
      const needle = norm(quoted ? quoted[1] : wanted);
      const side = /前段|left/i.test(wanted) ? 'left' : /后段|right/i.test(wanted) ? 'right' : '';
      const scored = candidates.map((item, index) => {
        const name = norm(label(item.el));
        let score = name === needle && needle ? 100 : name.startsWith(needle) && needle ? 70
          : name.includes(needle) && needle ? 55 : needle.includes(name) && name.length > 1 ? 35 : 0;
        const group = item.el.closest('.slot,.slot-card,[data-slot]');
        const context = String(group?.innerText || group?.getAttribute('data-slot') || '');
        if (score && side && context) {
          const matchesSide = side === 'left' ? /前段|left/i.test(context) : /后段|right/i.test(context);
          score = matchesSide ? score + 25 : 0;
        }
        return {...item, name, score, index};
      }).filter(item => item.score > 0).sort((a,b) => b.score-a.score || a.index-b.index);
      let winner = scored[0];
      if (!winner && inputs.length === 1 && /file|upload|import|json|csv|choose|选择|上传|导入|文件/i.test(wanted)) {
        const input = inputs[0];
        winner = candidates.find(item => item.el === input)
          || candidates.find(item => item.el.matches('label'))
          || candidates.find(item => item.el.matches('button,[role="button"]'));
      }
      if (!winner) return null;
      const before = winner.el.getBoundingClientRect();
      if (before.top < 12 || before.bottom > innerHeight - 12 || before.left < 12 || before.right > innerWidth - 12) {
        winner.el.scrollIntoView({behavior:'smooth',block:'nearest',inline:'nearest'});
        for (let frame=0; frame<12; frame++) await new Promise(resolve => requestAnimationFrame(resolve));
      }
      const r = winner.el.getBoundingClientRect(), x = r.left+r.width/2, y = r.top+r.height/2;
      const hit = document.elementFromPoint(x,y);
      if (!hit || (hit !== winner.el && !winner.el.contains(hit) && hit.getRootNode()?.host !== winner.el)) return null;
      return {x,y,width:r.width,height:r.height,tag:winner.el.tagName,name:winner.name || norm(label(winner.el)),
        fileIndex:inputs.indexOf(winner.input)};
    })()""" % json.dumps(target, ensure_ascii=False)


def _generic_script(side: str) -> str:
    side_offset = 0 if side == "A" else 1
    return """(() => {
      const visible = el => { const r=el.getBoundingClientRect(),s=getComputedStyle(el);
        return r.width>4&&r.height>4&&s.display!=='none'&&s.visibility!=='hidden'&&r.top<innerHeight&&r.bottom>0; };
      const unsafe = %s;
      const text = el => String(el.innerText||el.textContent||el.getAttribute('aria-label')||el.placeholder||'').trim();
      const controls = [...document.querySelectorAll('input:not([type="hidden"]),textarea,select,button,[role="button"],[role="tab"]')]
        .filter(visible).filter(el => !unsafe.some(word => text(el).toLowerCase().includes(word)));
      return controls.map(el => { const r=el.getBoundingClientRect(); return {
        type: el.matches('input,textarea') ? 'fill' : el.matches('select') ? 'select' : 'click',
        target: text(el), x:r.left+r.width/2, y:r.top+r.height/2, tag:el.tagName
      }; }).slice(%d, %d);
    })()""" % (json.dumps(list(UNSAFE_WORDS), ensure_ascii=False), side_offset, side_offset + 4)


def _pointer_path(start, end, duration):
    """A gently curved approach that eases in and slows near the control."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    bend = random.uniform(-1, 1) * min(65, distance * 0.18)
    nx, ny = (-dy / distance, dx / distance) if distance else (0, 0)
    c1 = (start[0] + dx * 0.3 + nx * bend, start[1] + dy * 0.3 + ny * bend)
    c2 = (start[0] + dx * 0.75 + nx * bend * 0.6, start[1] + dy * 0.75 + ny * bend * 0.6)
    steps = max(12, math.ceil(duration * 60))
    for index in range(1, steps + 1):
        t = 0.5 - math.cos(math.pi * index / steps) / 2
        yield tuple(
            (1-t)**3 * start[axis] + 3*(1-t)**2*t*c1[axis]
            + 3*(1-t)*t*t*c2[axis] + t**3*end[axis]
            for axis in (0, 1)
        )


def _warp_pointer(x: float, y: float, duration: float, bounds=None):
    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    core = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    core.CGEventCreate.restype = ctypes.c_void_p
    core.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    core.CGEventGetLocation.restype = CGPoint
    core.CGWarpMouseCursorPosition.argtypes = [CGPoint]
    event = core.CGEventCreate(None)
    start = core.CGEventGetLocation(event)
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    foundation.CFRelease(event)
    path = list(_pointer_path((start.x, start.y), (x, y), duration))
    started = time.monotonic()
    for index, point in enumerate(path, 1):
        if bounds:
            left, top, right, bottom = bounds
            point = (max(left, min(right, point[0])), max(top, min(bottom, point[1])))
        core.CGWarpMouseCursorPosition(CGPoint(*point))
        time.sleep(max(0, started + duration * index / len(path) - time.monotonic()))


def _pause(low, high, deadline=None):
    seconds = random.uniform(low, high)
    if deadline is not None and time.monotonic() + seconds >= deadline:
        raise RuntimeError("页面演示接近 90 秒上限，已保留现场；请缩短输入或减少操作后重录")
    time.sleep(seconds)


def _timed_pause(timing, key, fallback, deadline=None):
    """Pause around a per-recording target instead of reusing one fixed band."""
    seconds = float((timing or {}).get(key, fallback))
    _pause(max(0.04, seconds * 0.92), max(0.05, seconds * 1.08), deadline)


def _recording_cadence(actions, rng=None):
    """Create a distinct, bounded rhythm for every action in one recording."""
    rng = rng or random
    config = _cadence_config()
    tempo = rng.uniform(*config["tempo"])
    shapes = [0.78, 0.9, 1.03, 1.16, 1.28]
    rng.shuffle(shapes)
    bases = {name: float(value) for name, value in config["reading_bases"].items()}
    result_words = ("生成", "计算", "校准", "预览", "结果", "保存", "提交", "运行",
                    "generate", "calculate", "preview", "result", "save", "submit", "run")
    plan = []
    previous = None
    for index, action in enumerate(actions):
        action_type = str(action.get("type") or "click")
        description = " ".join(str(action.get(key) or "") for key in ("target", "purpose")).casefold()
        reading = bases.get(action_type, bases["click"]) * tempo * shapes[index % len(shapes)]
        if action_type == "click" and any(word in description for word in result_words):
            reading += rng.uniform(*config["result_extra"])
        if index == len(actions) - 1:
            reading *= rng.uniform(0.62, 0.78)  # the recorder holds the final frame separately
        reading = max(0.16, min(3.8, reading))
        if previous is not None and abs(reading - previous) < 0.07:
            reading = max(0.16, min(3.8, reading + (0.09 if index % 2 else -0.09)))
        previous = reading
        move_chance = {"fill": 0.22, "select": 0.3, "upload": 0.46}.get(action_type, 0.72)
        upload_timing = action_type == "upload"
        timing_ranges = {
            "opening": "upload_opening" if upload_timing else "opening",
            "browser_focus": "upload_browser_focus" if upload_timing else "browser_focus",
            **{key: key for key in (
                "picker_open", "picker_retry", "picker_shortcut", "picker_path",
                "picker_folder", "picker_select", "picker_open_button",
                "picker_select_move", "picker_open_button_move", "upload_settle",
            )},
        }
        plan.append({
            "profile": round(tempo, 3),
            "reading": round(reading, 3),
            "reading_move": rng.random() < move_chance,
            "reading_second_move": action_type == "click" and rng.random() < 0.24,
            "reading_move_seconds": round(rng.uniform(0.4, 0.65) * tempo, 3),
            **{key: round(rng.uniform(*config[source]) * tempo, 3)
               for key, source in timing_ranges.items()},
        })
    # Keep the demonstration from looking mechanically point-to-point. At
    # least one action gets a small reading/inspection movement, while the
    # chosen action, direction, distance, and duration still vary per video.
    if plan and not any(item["reading_move"] for item in plan):
        plan[rng.randrange(len(plan))]["reading_move"] = True
    return plan


def _move(cdp, x, y, rect, duration, deadline=None):
    if deadline is not None and time.monotonic() + duration >= deadline:
        raise RuntimeError("页面演示接近 90 秒上限，已保留现场；请减少操作后重录")
    left, top, width, height = rect
    x, y = max(4, min(width - 4, x)), max(4, min(height - 4, y))
    # Native cursor animation must not wait for a synchronous CDP round trip
    # every frame. Sync the final hover position before the next input instead.
    _warp_pointer(left+x, top+y, duration, bounds=(left+4, top+4, left+width-4, top+height-4))
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    return x, y


def _initial_pointer_script(points):
    """Read-only hit testing: prefer blank space, not a control or text."""
    return """(() => {
      const points = %s;
      const interactive = 'a,button,input,textarea,select,label,summary,iframe,' +
        '[contenteditable="true"],[onclick],[tabindex]:not([tabindex="-1"]),' +
        '[role="button"],[role="link"],[role="tab"],[role="checkbox"],' +
        '[role="switch"],[role="combobox"],[role="slider"],[role="menuitem"]';
      const blank = (x, y) => {
        const el = document.elementFromPoint(x, y);
        if (!el || el.closest(interactive) || el.isContentEditable) return false;
        if (['pointer','text','crosshair','grab','grabbing'].includes(getComputedStyle(el).cursor)) return false;
        for (const node of el.childNodes) {
          if (node.nodeType !== 3 || !node.textContent.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          if ([...range.getClientRects()].some(r => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom)) return false;
        }
        return true;
      };
      return points.find(p => [[0,0],[-12,0],[12,0],[0,-12],[0,12]]
        .every(([dx,dy]) => blank(p.x+dx, p.y+dy))) || null;
    })()""" % json.dumps(points)


def place_initial_pointer(cdp: CDP, capture_rect: str):
    """Position before recording, without clicking, scrolling or changing focus."""
    rect = tuple(int(value) for value in capture_rect.split(","))
    _, _, width, height = rect
    points = [{"x": width*x, "y": height*y}
              for x in (0.16, 0.32, 0.5, 0.68, 0.84)
              for y in (0.25, 0.42, 0.6, 0.78)]
    random.shuffle(points)
    point = cdp.evaluate(_initial_pointer_script(points))
    # Dense canvas-like pages may have no blank patch. Start at the page's left
    # edge in that case, without clicking anything or modifying the product.
    if not isinstance(point, dict):
        point = {"x": 8, "y": height*0.5}
    position = _move(cdp, float(point["x"]), float(point["y"]), rect, 0)
    _pause(0.15, 0.25)
    return position


def _approach(cdp, x, y, rect, origin, deadline):
    distance = math.dist(origin, (x, y)) if origin else 250
    duration = min(1.05, max(0.38, distance / 720)) * random.uniform(0.9, 1.15)
    # Occasional small adjustment on the way to a control, without an extra
    # click or a long stop between the two parts of the motion.
    enough_time = deadline is None or deadline-time.monotonic() > duration+0.8
    if distance > 100 and enough_time and random.random() < 0.38:
        _move(cdp, x+random.uniform(-25, 25), y+random.uniform(-16, 16), rect, duration*0.75, deadline)
        _move(cdp, x, y, rect, duration*0.25+0.12, deadline)
    else:
        _move(cdp, x, y, rect, duration, deadline)
    _pause(0.10, 0.26, deadline)


def _reading_pause(cdp, x, y, rect, deadline, action_type="click", last=False, timing=None):
    # Adjacent fields form one sequence; reserve longer reading time for a
    # submitted result. The recorder itself holds the final frame for 3–4 s.
    # A submitted final action can leave the page busy or unresponsive. The
    # visual hold needs no more CDP input, and a cosmetic hover must not turn
    # an already captured result into a failed recording.
    if last:
        if timing is not None:
            _timed_pause(timing, "reading", float(timing["reading"]), deadline)
        else:
            _pause(0.16, 0.35, deadline)
        return x, y
    if timing is not None:
        reading = float(timing["reading"])
        move = bool(timing.get("reading_move")) and (deadline is None or deadline-time.monotonic() > 1.0)
        if move:
            _pause(max(0.04, reading * 0.37), max(0.05, reading * 0.47), deadline)
        else:
            _timed_pause(timing, "reading", reading, deadline)
        point = (x, y)
        if move:
            point = _move(cdp, x+random.uniform(-55, 55), y+random.uniform(16, 45), rect,
                          float(timing["reading_move_seconds"]), deadline)
            if timing.get("reading_second_move"):
                point = _move(cdp, point[0]+random.uniform(-20, 20), point[1]+random.uniform(-10, 14),
                              rect, max(0.16, float(timing["reading_move_seconds"]) * 0.62), deadline)
            _pause(max(0.04, reading * 0.43), max(0.05, reading * 0.58), deadline)
        return point
    short = action_type in {"fill", "select"} or last
    _pause(0.16 if short else 0.4, 0.35 if short else 0.85, deadline)
    point = (x, y)
    enough_time = deadline is None or deadline-time.monotonic() > 1.3
    if enough_time and random.random() < (0.3 if short else 0.85):
        point = _move(cdp, x+random.uniform(-55, 55), y+random.uniform(16, 45),
                      rect, random.uniform(0.24, 0.45), deadline)
        if not short and random.random() < 0.3:
            point = _move(cdp, point[0]+random.uniform(-20, 20), point[1]+random.uniform(-10, 14),
                          rect, random.uniform(0.18, 0.3), deadline)
    if not short:
        _pause(0.35, 0.7, deadline)
    return point


def _click(cdp: CDP, x: float, y: float):
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    cdp.call("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
    })
    time.sleep(random.uniform(0.08, 0.16))
    cdp.call("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
    })


def _drag(cdp: CDP, resolved: dict, value: str):
    """Drag across a canvas using normalized start/end positions."""
    try:
        fractions = [float(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise RuntimeError("拖动画布需提供起止位置 x1,y1,x2,y2") from exc
    if len(fractions) != 4 or any(not 0 < part < 1 for part in fractions):
        raise RuntimeError("拖动画布的四个坐标必须在 0 与 1 之间")
    if fractions[:2] == fractions[2:]:
        raise RuntimeError("拖动画布的起点与终点不能相同")
    width, height = float(resolved["width"]), float(resolved["height"])
    left = float(resolved["x"]) - width / 2
    top = float(resolved["y"]) - height / 2
    x1, y1 = left + fractions[0] * width, top + fractions[1] * height
    x2, y2 = left + fractions[2] * width, top + fractions[3] * height
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x1, "y": y1})
    cdp.call("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x1, "y": y1, "button": "left", "clickCount": 1,
    })
    for step in range(1, 9):
        fraction = step / 8
        cdp.call("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x1 + (x2-x1)*fraction,
            "y": y1 + (y2-y1)*fraction, "button": "left", "buttons": 1,
        })
        time.sleep(0.025)
    cdp.call("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x2, "y": y2, "button": "left", "clickCount": 1,
    })


def _text_chunks(value, rng=None):
    """Latin text arrives by character; CJK input commits in short words."""
    rng = rng or random
    index = 0
    while index < len(value):
        size = 1
        if '\u3400' <= value[index] <= '\u9fff':
            while size < 3 and index + size < len(value) and '\u3400' <= value[index+size] <= '\u9fff':
                size += 1
            size = rng.randint(1, size)
        yield value[index:index+size]
        index += size


def _typing_schedule(value, rng=None):
    """Alternate short fluent bursts and slower checks, not uniform jitter."""
    rng = rng or random
    tempo = rng.uniform(0.9, 1.15)
    fast_left, slow_left = rng.randint(2, 5), 0
    previous = ""
    for index, chunk in enumerate(_text_chunks(value, rng)):
        if index == 0:
            delay = rng.uniform(0.25, 0.45)
        elif slow_left:
            delay = rng.uniform(0.18, 0.38)
            slow_left -= 1
            if not slow_left:
                fast_left = rng.randint(2, 5)
        else:
            delay = rng.uniform(0.05, 0.11)
            fast_left -= 1
            if not fast_left:
                slow_left = rng.randint(1, 2)
        # Pause before continuing the next phrase, not after every character.
        if previous and previous[-1] in "。！？.!?;；\n":
            delay += rng.uniform(0.30, 0.60)
        elif previous and (previous[-1].isspace() or previous[-1] in ",，:：、"):
            delay += rng.uniform(0.10, 0.25)
        yield chunk, delay * tempo
        previous = chunk


def _key(cdp, key, code=None, modifiers=0, commands=None):
    keycode = {"a": 65, "Backspace": 8, "Tab": 9, "Escape": 27, "F2": 113,
               " ": 32, "Home": 36, "ArrowDown": 40, "Enter": 13}[key]
    for kind in ("keyDown", "keyUp"):
        params = {"type": kind, "key": key, "code": code or key,
                  "modifiers": modifiers, "windowsVirtualKeyCode": keycode}
        # CDP does not run macOS's native shortcut mapping. Meta+A alone can
        # leave a prefilled React textarea unchanged; explicitly ask Blink to
        # execute its native editing command, just as a real Select All does.
        if commands and kind == "keyDown":
            params["commands"] = commands
        cdp.call("Input.dispatchKeyEvent", params)


def _should_paste(value: str, target: str = "", context: str = "") -> bool:
    """Paste multiline/long text and technical payloads; pace short prose."""
    text = value.strip()
    if not text:
        return False
    # People copy prepared paragraphs and multiline examples as a whole,
    # including grammar rules and ordinary descriptions with spaces.
    if "\n" in text or "\r" in text or len(text) >= LONG_TEXT_PASTE_THRESHOLD:
        return True
    field_context = " ".join(part for part in (target, context) if part)
    if PASTE_CONTEXT_RE.search(field_context):
        return True
    if re.search(r"命令|脚本|代码|路径|网址|\b(?:command|shell|script|code|sql|json|path|url)\b", field_context, re.I):
        return True
    if text.startswith(("[", "{")):
        try:
            if isinstance(json.loads(text), (list, dict)):
                return True
        except (ValueError, TypeError):
            pass
    if re.fullmatch(r"https?://\S+", text, re.I):
        return True
    if re.match(r"^(?:/|\./|\.\./|~/|[A-Za-z]:[\\/])", text) and "\n" not in text:
        return True
    # GS1/barcode source text, pair numbers, repository slugs, hashes and
    # similar dense values should appear all at once. Do not restrict their
    # punctuation: real labels commonly contain parentheses and separators.
    # Short sentences with whitespace remain on the paced path.
    if len(text) >= LONG_TOKEN_PASTE_THRESHOLD and not re.search(r"\s", text):
        return True
    # Avoid interpreting descriptive prose as a command solely because its
    # first word happens to be an executable name (e.g. 'go to the park').
    if re.search(r"备注|描述|说明|名称|姓名|\b(?:notes?|description|comments?|name)\b", target, re.I):
        return False
    if re.match(r"^(?:[A-Za-z_]\w*=\S+\s+)*(?:sudo\s+)?(?:curl|wget|docker|git|npm|pnpm|yarn|bun|python3?|pip3?|node|pytest|go|cargo|kubectl|make|cmake|mvn|gradle|dotnet|java|ls|cd|cat|grep|rg|jq|sed|awk|echo|printf|bash|sh|zsh|ffmpeg)(?:\s|$)", text):
        return True
    return bool(re.match(
        r"^(?:```|(?:SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+.+\s+SET|DELETE\s+FROM|CREATE\s+TABLE)\b|(?:def|function|func)\s+\w+\s*\(|(?:const|let|var)\s+\w+\s*=|from\s+[\w.]+\s+import\s|import\s+[\w.{])",
        text, re.I | re.S,
    ))


def _fill(cdp: CDP, value: str, deadline=None, target: str = "", context: str = ""):
    # Focus inside a Web Component lives on its shadow root; document.activeElement
    # is only the host and has no input value to verify.
    active_value_js = """document.activeElement && (() => {
      let el = document.activeElement;
      while (el?.shadowRoot?.activeElement) el = el.shadowRoot.activeElement;
      return el?.value ?? el?.textContent ?? '';
    })()"""
    paste = _should_paste(value, target, context)
    if paste:
        # Reserve the whole human-paced paste sequence before changing the
        # focused control. A random short first pause must not let select-all
        # and delete happen only for the following pause to hit the deadline.
        required = PASTE_FOCUS_PAUSE[1] + 0.3 + PASTE_SETTLE_PAUSE[1]
        if deadline is not None and time.monotonic() + required >= deadline:
            raise RuntimeError("页面演示接近 90 秒上限，已保留现场；请缩短输入或减少操作后重录")
        # A human first settles on the focused field, then pastes the complete
        # approved term. The content itself appears at once, but never in the
        # same frame as the click or the following action.
        _pause(*PASTE_FOCUS_PAUSE, deadline)
    numeric_field = cdp.evaluate("""(() => {
      let el = document.activeElement;
      while (el?.shadowRoot?.activeElement) el = el.shadowRoot.activeElement;
      return el?.type === 'number';
    })()""") is True
    _key(cdp, "a", "KeyA", modifiers=4, commands=["selectAll"])
    _pause(0.15, 0.3, deadline)
    if numeric_field and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        # Replacing the selection in one native edit avoids an intermediate
        # empty value that can mutate dependent controls (such as K <= N).
        cdp.call("Input.insertText", {"text": value})
        _pause(*PASTE_SETTLE_PAUSE, deadline)
        actual = cdp.evaluate(active_value_js)
        if actual != value:
            raise RuntimeError(f"输入控件“{target}”未完整接受演示内容（预期 {len(value)} 字符，实际 {len(str(actual))} 字符）")
        return "paste"
    _key(cdp, "Backspace")
    cleared = cdp.evaluate(active_value_js)
    # Controlled numeric inputs may normalize an empty edit immediately (for
    # example, React turns an empty point count into its minimum value 1). If
    # that normalized value is already exactly what the plan requested, the
    # visible select-all/delete edit succeeded and no further typing is needed.
    if str(cleared) == value:
        _pause(0.18, 0.4, deadline)
        return "paste" if paste else "typing"
    if cleared != "":
        # Controlled number fields may normalize an empty edit to their
        # current minimum rather than zero. Replace that visible fallback in
        # one native input operation. Ordinary text fields still fail closed.
        if (str(cleared) == "0" or numeric_field) and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            _key(cdp, "a", "KeyA", modifiers=4, commands=["selectAll"])
            _pause(0.15, 0.3, deadline)
            cdp.call("Input.insertText", {"text": value})
            _pause(*PASTE_SETTLE_PAUSE, deadline)
            actual = cdp.evaluate(active_value_js)
            if actual != value:
                raise RuntimeError(f"输入控件“{target}”未完整接受演示内容（预期 {len(value)} 字符，实际 {len(str(actual))} 字符）")
            return "paste"
        raise RuntimeError(f"输入控件“{target}”未能清空旧内容（剩余 {len(str(cleared))} 字符），已停止输入")
    if numeric_field and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        # Typing the digits separately can make a controlled field clamp an
        # intermediate value before the full number is present.
        cdp.call("Input.insertText", {"text": value})
        _pause(*PASTE_SETTLE_PAUSE, deadline)
        actual = cdp.evaluate(active_value_js)
        if actual != value:
            raise RuntimeError(f"输入控件“{target}”未完整接受演示内容（预期 {len(value)} 字符，实际 {len(str(actual))} 字符）")
        return "paste"
    if paste:
        # One input operation, without overwriting the user's OS clipboard or
        # executing an Enter key. Keep all whitespace and newlines verbatim.
        cdp.call("Input.insertText", {"text": value})
        _pause(*PASTE_SETTLE_PAUSE, deadline)
    else:
        for chunk, delay in _typing_schedule(value):
            _pause(delay, delay, deadline)
            cdp.call("Input.insertText", {"text": chunk})
        _pause(0.18, 0.4, deadline)
    actual = cdp.evaluate(active_value_js)
    if actual != value:
        raise RuntimeError(f"输入控件“{target}”未完整接受演示内容（预期 {len(value)} 字符，实际 {len(str(actual))} 字符）")
    # Keep focus here. The next real mouse click commits change/blur and
    # focuses its target; Tab would highlight that target before the cursor
    # has arrived and make the recording look out of sync.
    return "paste" if paste else "typing"


def _select_first_available(value: str) -> bool:
    """Recognize plan wording that asks for any usable option, not a label."""
    return bool(re.search(r"(?:第一个|首个).*可用|first\s+available", value, re.IGNORECASE))


def _select(cdp: CDP, target: str, value: str, deadline=None):
    # Work on the control actually clicked, including selects labelled by <label>.
    token = f"select-{time.time_ns():x}"
    choice_script = """(() => {
      const el=document.activeElement, requested=%s.toLowerCase(), token=%s, firstAvailable=%s;
      if(!el || el.tagName!=='SELECT') return null;
      const options=[...el.options].filter(item => !item.disabled && !item.parentElement.disabled);
      const option=options.find(item => item.value.toLowerCase()===requested ||
        (item.textContent||'').trim().toLowerCase()===requested)
        || (requested && options.find(item => (item.textContent||'').toLowerCase().includes(requested)))
        || (firstAvailable && options.find(item => item.value))
        || (!requested && options.find(item => item.value));
      if(!option) return null;
      el.setAttribute('data-pairwise-select-token',token);
      return {steps:options.indexOf(option),value:option.value};
    })()""" % (json.dumps(value, ensure_ascii=False), json.dumps(token),
                 json.dumps(_select_first_available(value)))
    # Permission/device enumeration often finishes just after the select first
    # becomes visible. Keep the already-clicked control open and wait for a
    # usable option instead of failing on its disabled placeholder.
    choice = None
    choice_until = min(time.monotonic() + 6, deadline) if deadline is not None else time.monotonic() + 6
    while not choice and time.monotonic() < choice_until:
        choice = cdp.evaluate(choice_script)
        if not choice:
            _pause(0.12, 0.22, deadline)
    if not choice:
        raise RuntimeError(f"无法在“{target}”中选择“{value}”，现场已保留")
    # CDP key events do not change native <select> controls reliably on macOS,
    # even though the element has focus. Close the visible menu, then apply the
    # chosen option through the native value setter and dispatch the same input
    # and change events that application code observes after a user selection.
    _pause(0.12, 0.3, deadline)
    _key(cdp, "Escape")
    _pause(0.12, 0.3, deadline)
    accepted = cdp.evaluate("""(() => {
      const token=%s, value=%s;
      const el=[...document.querySelectorAll('[data-pairwise-select-token]')]
        .find(item => item.getAttribute('data-pairwise-select-token')===token);
      if(!el || el.tagName!=='SELECT') return null;
      const option=[...el.options].find(item => item.value===value && !item.disabled);
      if(!option) return null;
      const setter=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;
      setter.call(el,value);
      if(el.value!==value) return null;
      el.dispatchEvent(new Event('input',{bubbles:true}));
      el.dispatchEvent(new Event('change',{bubbles:true}));
      return {value:el.value, resetByHandler:value!=='' && el.value==='' &&
        el.options[0]?.value===''};
    })()""" % (json.dumps(token), json.dumps(choice["value"], ensure_ascii=False)))
    _pause(0.16, 0.34, deadline)
    actual = cdp.evaluate("""(() => {
      const token=%s;
      const el=[...document.querySelectorAll('[data-pairwise-select-token]')]
        .find(item => item.getAttribute('data-pairwise-select-token')===token);
      const value=el?.value ?? null;
      el?.removeAttribute('data-pairwise-select-token');
      return value;
    })()""" % json.dumps(token))
    selected = choice["value"]
    if not accepted or not (
        accepted["value"] == selected and actual == selected
        or accepted["resetByHandler"] and actual == ""
    ):
        raise RuntimeError(f"“{target}”没有接受所选值，现场已保留")


def _resolved_action(cdp: CDP, action: dict):
    """Correct old plans that tried to paste file contents into an upload control."""
    action = dict(action)
    action_type = str(action.get("type") or "click")
    target = str(action.get("target") or "")
    if action_type == "upload":
        return action, cdp.evaluate(_upload_target_script(target))
    resolved = cdp.evaluate(_resolve_script(target, action_type))
    if (action_type == "fill" and not resolved and action.get("value")
            and re.search(r"file|upload|import|json|csv|choose|选择|上传|导入|文件", target, re.I)):
        upload = cdp.evaluate(_upload_target_script(target))
        if upload:
            action["type"] = "upload"
            return action, upload
    return action, resolved


def preflight(cdp: CDP, plan: dict) -> dict:
    """Reject an unusable first step before capture, without changing the page."""
    result = dict(plan)
    actions = [dict(item) for item in (plan.get("actions") or [])[:MAX_ACTIONS]]
    if not actions:
        raise RuntimeError("页面演示路线没有可执行动作；录制未开始")
    for index, action in enumerate(actions):
        if action.get("type") not in {"click", "fill", "select", "upload", "drag"}:
            raise RuntimeError(f"页面演示第 {index+1} 步的动作类型无效")
        if action.get("type") == "upload" and not str(action.get("value") or ""):
            raise RuntimeError(f"页面演示第 {index+1} 步缺少待上传的文件内容")
        # A file control may only appear after an earlier navigation click.
        # Validate the first step now; resolve later controls at execution time.
        if index == 0:
            normalized, resolved = _resolved_action(cdp, action)
            actions[index] = normalized
            if not resolved:
                raise RuntimeError(f"页面演示首步“{action.get('target') or ''}”找不到可操作控件；录制未开始")
    if (sys.platform == "darwin" and any(item.get("type") == "upload" for item in actions)
            and not _native_picker_available()):
        raise RuntimeError(
            f"{_native_picker_permission_hint()}；当前未获授权，"
            "文件选择窗口无法自动操作；录制未开始"
        )
    result["actions"] = actions
    return result


def _upload_name(action: dict, index: int) -> str:
    requested = str(action.get("file_name") or "").strip()
    if requested:
        if (Path(requested).name != requested or requested.startswith(".") or ".." in requested
                or not re.fullmatch(r"[\w.\-]{1,80}", requested)):
            raise RuntimeError("上传文件名无效，必须是简短文件名，不得包含路径")
        return requested
    content = str(action.get("value") or "")
    try:
        parsed = json.loads(content)
    except ValueError:
        extension = "json" if re.search(r"json", str(action.get("target") or ""), re.I) else "txt"
        return f"source-{index+1}.{extension}"
    stem = "cues" if isinstance(parsed, dict) and "cues" in parsed else "source"
    return f"{stem}-{index+1}.json"


def _assert_picker_foreground(browser_pid=None):
    # macOS may report the user's original Chrome process as the frontmost
    # application even when the separate-profile app window belongs to a
    # second Chrome PID. The picker itself is resolved against browser_pid.
    last_error = None
    for attempt in range(3):
        try:
            return native_picker.front_chrome_pid()
        except RuntimeError as exc:
            last_error = exc
        if browser_pid is None or attempt == 2:
            break
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to set frontmost of first application process whose unix id is %d to true' % browser_pid],
                text=True, capture_output=True, timeout=8, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.22)
    raise last_error or RuntimeError("文件窗口已不在录屏浏览器前台，已停止选择")


def _activate_recording_browser(browser_pid, deadline=None, timing=None):
    if browser_pid is None:
        return
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to set frontmost of first application process whose unix id is %d to true' % browser_pid],
            text=True, capture_output=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法切换到录屏浏览器：{exc}") from exc
    if result.returncode:
        raise RuntimeError("无法切换到录屏浏览器的文件窗口")
    _timed_pause(timing, "browser_focus", 0.86, deadline)
    _assert_picker_foreground(browser_pid)


def _native_picker_available() -> bool:
    if sys.platform != "darwin":
        return False
    services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    services.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(services.AXIsProcessTrusted())


def _native_key_event(code: int, down: bool, *, flags=0, character=""):
    core = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    core.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    core.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    core.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    core.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
    core.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    units = character.encode("utf-16-le") if character else b""
    encoded = ((ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units) if units else None)
    event = core.CGEventCreateKeyboardEvent(None, code, down)
    if not event:
        raise RuntimeError("无法创建 macOS 键盘事件")
    try:
        core.CGEventSetFlags(event, flags)
        if encoded is not None:
            core.CGEventKeyboardSetUnicodeString(event, len(encoded), encoded)
        core.CGEventPost(0, event)
    finally:
        foundation.CFRelease(event)


def _native_key(code: int, *, flags=0, character=""):
    _native_key_event(code, True, flags=flags, character=character)
    time.sleep(0.02)
    _native_key_event(code, False, flags=flags, character=character)


def _native_go_to_folder():
    shift, command = 0x20000, 0x100000
    # Post real modifier down/up events: a flagged G event alone is ignored
    # by some native macOS file panels.
    for code, down, flags in (
        (56, True, shift), (55, True, shift | command),
        (5, True, shift | command), (5, False, shift | command),
        (55, False, shift), (56, False, 0),
    ):
        _native_key_event(code, down, flags=flags)
        time.sleep(0.02)


def _native_paste_path(path: Path):
    try:
        subprocess.run(
            ["pbcopy"], input=str(path.resolve()), text=True, encoding="utf-8",
            capture_output=True, timeout=5, check=True,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"无法复制文件路径到剪贴板：{exc}") from exc
    command = 0x100000
    for code, down, flags in (
        (55, True, command), (9, True, command),
        (9, False, command), (55, False, 0),
    ):
        _native_key_event(code, down, flags=flags)
        time.sleep(0.02)


def _native_mouse_click(x: float, y: float, deadline=None, timing=None, timing_prefix="picker_select"):
    """Move onto an actual Open-panel control and click it visibly."""
    duration = (float(timing[f"{timing_prefix}_move"])
                if timing and f"{timing_prefix}_move" in timing else random.uniform(0.25, 0.45))
    if deadline is not None and time.monotonic() + duration >= deadline:
        raise RuntimeError("页面演示接近 90 秒上限，已保留文件窗口")
    _warp_pointer(x, y, duration)
    _timed_pause(timing, timing_prefix, 0.18, deadline)
    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
    core = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    core.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    core.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    core.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    for kind in (1, 2):  # kCGEventLeftMouseDown, kCGEventLeftMouseUp
        event = core.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), 0)
        if not event:
            raise RuntimeError("无法创建 macOS 鼠标点击事件")
        try:
            core.CGEventPost(0, event)
        finally:
            foundation.CFRelease(event)
        time.sleep(0.08)


def _dismiss_login_browser_prompt(browser_pid=None, deadline=None) -> bool:
    """Click away Chrome's password warning before the product demo continues."""
    if browser_pid is None or not _native_picker_available():
        return False
    until = min(time.monotonic() + 1.8, deadline) if deadline is not None else time.monotonic() + 1.8
    while time.monotonic() < until:
        try:
            point = native_picker.button_center(("确定",), browser_pid)
        except (OSError, RuntimeError):
            return False
        if point:
            _assert_picker_foreground(browser_pid)
            _native_mouse_click(*point, deadline=deadline, timing_prefix="browser_prompt")
            _pause(0.18, 0.32, deadline)
            return True
        time.sleep(0.1)
    return False


def _picker_selected_name(cdp: CDP, file_index: int) -> str:
    return str(cdp.evaluate("""(() => {
      const input = document.querySelectorAll('input[type="file"]')[%d];
      return window.__pairwiseUploadObserved || input?.files?.[0]?.name || '';
    })()""" % file_index) or "")


def _choose_native_file(cdp: CDP, path: Path, file_index: int, deadline=None, browser_pid=None,
                        timing=None, selected_paths=None):
    """Use the current folder when possible; otherwise navigate before clicking."""
    # Chrome may report the chooser before its native panel has keyboard
    # focus. A premature Command-Shift-G is handled by the browser instead.
    _timed_pause(timing, "picker_open", 0.68, deadline)
    _assert_picker_foreground(browser_pid)
    if not _native_picker_available():
        raise RuntimeError(f"{_native_picker_permission_hint()}；当前未获授权，无法操作文件选择窗口")
    # A subsequent upload often reopens the chooser in the same folder.
    # The exact file URL/name in the active Open panel is safer than relying
    # on the displayed folder label, which may be shared by different paths.
    controls = native_picker.picker_controls(path, browser_pid)
    for attempt in range(2) if not controls else ():
        if attempt:
            _timed_pause(timing, "picker_retry", 0.58, deadline)
            _assert_picker_foreground(browser_pid)
        _native_go_to_folder()
        _timed_pause(timing, "picker_shortcut", 0.58, deadline)
        _native_paste_path(path.parent)
        _timed_pause(timing, "picker_path", 0.62, deadline)
        _native_key(36)  # Confirm path in the Go to Folder sheet
        _timed_pause(timing, "picker_folder", 0.33, deadline)
        # Finder can take a moment to populate an Open panel after changing
        # folders.  Poll the exact file URL instead of treating the first AX
        # snapshot as a definitive "file missing" result.
        visible_until = min(time.monotonic() + 2.5, deadline) if deadline is not None else time.monotonic() + 2.5
        while time.monotonic() < visible_until:
            controls = native_picker.picker_controls(path, browser_pid)
            if controls:
                break
            time.sleep(0.15)
        if controls:
            break
    if not controls:
        raise RuntimeError(f"文件选择窗口未显示 {path.name}，已停止录制")
    _assert_picker_foreground(browser_pid)
    _timed_pause(timing, "picker_select", 0.18, deadline)
    _native_mouse_click(*controls[0], deadline=deadline, timing=timing,
                        timing_prefix="picker_select")
    if selected_paths and len(selected_paths) > 1:
        # Multi-file fixtures occupy a fresh folder containing exactly this
        # batch. Select all with a real keyboard gesture in the Open panel.
        command = 0x100000
        for code, down, flags in (
            (55, True, command), (0, True, command),
            (0, False, command), (55, False, 0),
        ):
            _native_key_event(code, down, flags=flags)
            time.sleep(0.02)
    ready_until = min(time.monotonic() + 1.5, deadline) if deadline is not None else time.monotonic() + 1.5
    while time.monotonic() < ready_until and not native_picker.open_button_ready(browser_pid):
        time.sleep(0.1)
    if not native_picker.open_button_ready(browser_pid):
        raise RuntimeError(f"选中 {path.name} 后‘打开’按钮没有就绪，已停止录制")
    _timed_pause(timing, "picker_open_button", 0.19, deadline)
    _assert_picker_foreground(browser_pid)
    _native_mouse_click(*controls[1], deadline=deadline, timing=timing,
                        timing_prefix="picker_open_button")
    accepted_until = min(time.monotonic() + 2.0, deadline) if deadline is not None else time.monotonic() + 2.0
    while time.monotonic() < accepted_until:
        if selected_paths and len(selected_paths) > 1:
            names = cdp.evaluate("window.__pairwiseUploadObservedFiles || []") or []
            if set(names) == {item.name for item in selected_paths}:
                return
        if _picker_selected_name(cdp, file_index) == path.name:
            if not selected_paths or len(selected_paths) == 1:
                return
        time.sleep(0.1)
    raise RuntimeError(f"文件窗口没有打开 {path.name}，已停止录制")


def _write_upload_fixture(path: Path, content: str):
    if not content or len(content.encode("utf-8")) > 256_000:
        raise RuntimeError("上传演示文件内容为空或超过 256 KB")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Recording plans carry small binary fixtures through JSON as one code
    # point per byte.  Preserve those bytes for WAV uploads instead of
    # expanding values above 0x7f as UTF-8, which corrupts RIFF chunk sizes.
    image_match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", content)
    if image_match and path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        if (image_match.group(1) == "png") != (path.suffix.lower() == ".png"):
            raise RuntimeError("图片演示文件扩展名与内容格式不一致")
        try:
            image_bytes = base64.b64decode(image_match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("图片演示文件不是有效的 base64 内容") from exc
        signature = b"\x89PNG\r\n\x1a\n" if image_match.group(1) == "png" else b"\xff\xd8"
        if not image_bytes.startswith(signature):
            raise RuntimeError("图片演示文件头与声明格式不一致")
        path.write_bytes(image_bytes)
    elif path.suffix.lower() == ".pcap" and content.startswith("data:application/vnd.tcpdump.pcap;base64,"):
        try:
            capture = base64.b64decode(content.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("PCAP 演示文件不是有效的 base64 内容") from exc
        if len(capture) < 24 or capture[:4] not in {b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"}:
            raise RuntimeError("PCAP 演示文件头无效")
        path.write_bytes(capture)
    elif path.suffix.lower() == ".wav" and content.startswith("data:audio/"):
        audio_match = re.fullmatch(r"data:audio/(?:wav|x-wav|wave);base64,([A-Za-z0-9+/=]+)", content)
        if not audio_match:
            raise RuntimeError("WAV 演示文件需要有效的 audio/wav base64 内容")
        try:
            audio_bytes = base64.b64decode(audio_match.group(1), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("WAV 演示文件不是有效的 base64 内容") from exc
        if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
            raise RuntimeError("WAV 演示文件头无效")
        path.write_bytes(audio_bytes)
    elif path.suffix.lower() == ".wav" and content.startswith("RIFF"):
        path.write_bytes(content.encode("latin-1"))
    else:
        path.write_text(content, encoding="utf-8")


def _upload(cdp: CDP, action: dict, file_index: int, path: Path, x: float, y: float,
            deadline=None, native_picker=True, browser_pid=None, timing=None,
            activate_browser=True):
    """Import a real local file through the visible macOS picker when recording."""
    content = str(action.get("value") or "")
    paths = [path]
    try:
        bundle = json.loads(content)
    except (ValueError, TypeError):
        bundle = None
    if isinstance(bundle, dict) and "__pairwise_upload_files__" in bundle:
        files = bundle["__pairwise_upload_files__"]
        if not isinstance(files, list) or not 2 <= len(files) <= 6:
            raise RuntimeError("多文件演示需要 2 至 6 个文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="audio-demo-", dir=path.parent))
        paths = []
        for item in files:
            name = str(item.get("file_name") or "") if isinstance(item, dict) else ""
            if not re.fullmatch(r"[\w.\-]{1,80}", name) or name.startswith(".") or ".." in name:
                raise RuntimeError("多文件演示中的文件名无效")
            target = folder / name
            if target in paths:
                raise RuntimeError("多文件演示中的文件名重复")
            _write_upload_fixture(target, str(item.get("value") or ""))
            paths.append(target)
        path = paths[0]
    else:
        _write_upload_fixture(path, content)
    before = cdp.evaluate("document.body?.innerText || ''") or ""
    document = cdp.call("DOM.getDocument", {"depth": 1})["root"]["nodeId"]
    nodes = cdp.call("DOM.querySelectorAll", {
        "nodeId": document, "selector": 'input[type="file"]',
    })["nodeIds"]
    if not 0 <= file_index < len(nodes):
        raise RuntimeError("上传控件在操作前发生变化，已停止录制")
    # Some real apps clear the input immediately after reading the file. Track
    # the actual change event rather than assuming input.files stays populated.
    cdp.evaluate("""(() => {
      const input = document.querySelectorAll('input[type="file"]')[%d];
      window.__pairwiseUploadObserved = null;
      window.__pairwiseUploadObservedFiles = [];
      document.addEventListener('change', event => {
        if (event.target === input) {
          window.__pairwiseUploadObserved = event.target.files?.[0]?.name || null;
          window.__pairwiseUploadObservedFiles = [...(event.target.files || [])].map(file => file.name);
        }
      }, {capture:true, once:true});
    })()""" % file_index)
    if native_picker:
        if activate_browser:
            _activate_recording_browser(browser_pid, deadline, timing)
        _click(cdp, x, y)
        _choose_native_file(cdp, path, file_index, deadline, browser_pid, timing, selected_paths=paths)
    else:
        # The headless integration test has no native picker. Production
        # recordings always follow the visible chooser path above.
        cdp.call("DOM.setFileInputFiles", {
            "files": [str(item.resolve()) for item in paths], "nodeId": nodes[file_index],
        })
    observed = ""
    rendered = False
    accepted = False
    until = min(time.monotonic() + 6, deadline) if deadline is not None else time.monotonic() + 6
    while time.monotonic() < until:
        state = cdp.evaluate("""(() => {
          const input = document.querySelectorAll('input[type="file"]')[%d];
          return {file: window.__pairwiseUploadObserved || input?.files?.[0]?.name || '',
            files: window.__pairwiseUploadObservedFiles || [],
            text: document.body?.innerText || ''};
        })()""" % file_index) or {}
        observed = str(state.get("file") or "")
        rendered = str(state.get("text") or "") != before
        accepted = (set(state.get("files") or []) == {item.name for item in paths}
                    if len(paths) > 1 else observed == path.name)
        if accepted and rendered:
            break
        time.sleep(0.12)
    if not accepted:
        raise RuntimeError("页面没有接受上传文件，已停止录制")
    if not rendered:
        raise RuntimeError("页面收到文件但未显示导入结果，已停止录制")
    _timed_pause(timing, "upload_settle", 0.38, deadline)
    return str(path)


def _arm_rapid_followup(cdp: CDP, target: str):
    """Click a control during a short-lived enabled state after the next action.

    A real 80 ms transition cannot be followed by the normal pointer cadence.
    Arm a DOM observer before the initiating click so the app's own button is
    activated as soon as React enables it.
    """
    cdp.evaluate("""(() => {
      const wanted = %s;
      const norm = text => String(text || '').replace(/\\s+/g, ' ').trim();
      const state = {done:false};
      window.__pairwiseRapidFollowup = state;
      const observer = new MutationObserver(() => {
        if (state.done) return;
        const button = [...document.querySelectorAll('button')]
          .find(el => norm(el.innerText) === wanted && !el.disabled);
        if (button) {
          state.done = true;
          observer.disconnect();
          button.click();
        }
      });
      observer.observe(document.documentElement, {
        attributes:true, attributeFilter:['disabled'], childList:true, subtree:true
      });
      setTimeout(() => observer.disconnect(), 6000);
    })()""" % json.dumps(target, ensure_ascii=False))


def _wait_rapid_followup(cdp: CDP, target: str, deadline=None):
    end = min(time.monotonic() + 6, deadline) if deadline is not None else time.monotonic() + 6
    while time.monotonic() < end:
        if cdp.evaluate("Boolean(window.__pairwiseRapidFollowup?.done)"):
            return
        time.sleep(0.04)
    raise RuntimeError(f"短时可用的“{target}”控件未出现，现场已保留")


def run(port: int, plan: dict, side: str, log_path: Path, deadline=None, initial_pointer=None,
        expected_url=None, upload_directory: Path | None = None, browser_pid: int | None = None) -> str:
    cdp = CDP(port, expected_url) if expected_url else CDP(port)
    events = []
    action = None
    try:
        _wait_for_page(cdp, deadline=deadline, settle=False, expected_url=expected_url)
        capture_rect = viewport_rect(cdp)
        rect = tuple(int(value) for value in capture_rect.split(","))
        actions = list(plan.get("actions") or [])[:MAX_ACTIONS]
        if not actions:
            actions = cdp.evaluate(_generic_script(side)) or []
        cadence = _recording_cadence(actions)
        completed = 0
        _timed_pause(cadence[0] if cadence else None, "opening", 0.8, deadline)
        pointer = initial_pointer
        if upload_directory is not None:
            upload_directory.mkdir(parents=True, exist_ok=True)
        upload_scope = (nullcontext(str(upload_directory)) if upload_directory is not None else
                        tempfile.TemporaryDirectory(prefix="page-upload-", dir=log_path.parent))
        with upload_scope as upload_dir:
            for index, action in enumerate(actions):
                timing = cadence[index]
                target = str(action.get("target") or "")
                if any(word in target.casefold() for word in UNSAFE_WORDS):
                    continue
                action, resolved = _resolved_action(cdp, action)
                action_type = str(action.get("type") or "click")
                # Wait for the next real control instead of adding a fixed delay
                # after every operation. Never click stale fallback coordinates.
                # Full-stack pages may need a few seconds for an authenticated
                # navigation, worker/API fetch, or solver result before the
                # next control exists. Poll the live DOM instead of treating a
                # normal network/render delay as a broken demonstration.
                resolve_until = min(time.monotonic()+8, deadline) if deadline is not None else time.monotonic()+8
                while not resolved and time.monotonic() < resolve_until:
                    _pause(0.12, 0.22, deadline)
                    action, resolved = _resolved_action(cdp, action)
                    action_type = str(action.get("type") or "click")
                if not resolved:
                    events.append({"action": action, "status": "not-found"})
                    raise RuntimeError(f"页面演示第 {index+1} 步没有找到“{target}”控件，现场已保留")
                x, y = float(resolved["x"]), float(resolved["y"])
                x += random.uniform(-1, 1) * min(9, float(resolved.get("width", 0)) * 0.15)
                y += random.uniform(-1, 1) * min(4, float(resolved.get("height", 0)) * 0.15)
                x, y = max(4, min(rect[2]-4, x)), max(4, min(rect[3]-4, y))
                # Bringing Chrome to the front can take over a second on macOS.
                # Do it before the pointer approaches the upload control so the
                # recording never looks stalled with the cursor on the button.
                if action_type == "upload":
                    _activate_recording_browser(browser_pid, deadline, timing)
                _approach(cdp, x, y, rect, pointer, deadline)
                if action_type == "upload":
                    uploaded_path = _upload(cdp, action, int(resolved["fileIndex"]),
                            Path(upload_dir) / f"{side}-{index+1}-{_upload_name(action, index)}", x, y,
                            deadline, browser_pid=browser_pid, timing=timing,
                            activate_browser=False)
                elif action_type == "drag":
                    _drag(cdp, resolved, str(action.get("value") or ""))
                else:
                    rapid_target = str(action.get("rapid_followup_target") or "")
                    if rapid_target:
                        _arm_rapid_followup(cdp, rapid_target)
                    _click(cdp, x, y)
                    if rapid_target:
                        _wait_rapid_followup(cdp, rapid_target, deadline)
                    if (action_type == "click"
                            and re.search(r"(?:登录|登陆|login|log[ -]?in|sign[ -]?in)", target, re.I)):
                        _dismiss_login_browser_prompt(browser_pid, deadline)
                input_mode = ""
                if action_type == "fill":
                    if resolved.get("gridCell"):
                        _key(cdp, "F2")
                    _pause(0.15, 0.32, deadline)
                    input_mode = _fill(
                        cdp,
                        str(action.get("value") or ("演示 A" if side == "A" else "演示 B")),
                        deadline,
                        target=target,
                        context=" ".join(filter(None, (
                            str(resolved.get("name") or ""),
                            str(action.get("purpose") or ""),
                        ))),
                    )
                elif action_type == "select":
                    _select(cdp, target, str(action.get("value") or ""), deadline)
                completed += 1
                event = {"action": action, "status": "complete", "resolved": resolved.get("name", ""),
                         "timing": {"profile": timing["profile"], "reading": timing["reading"]}}
                if input_mode:
                    event["input_mode"] = input_mode
                if action_type == "upload":
                    event["uploaded_file"] = uploaded_path
                    event["page_updated"] = True
                if action.get("rapid_followup_target"):
                    event["rapid_followup_clicked"] = action["rapid_followup_target"]
                events.append(event)
                pointer = _reading_pause(cdp, x, y, rect, deadline, action_type,
                                         last=index==len(actions)-1, timing=timing)
        if completed == 0:
            raise RuntimeError("页面中没有找到可演示的控件")
        return capture_rect
    except Exception as exc:
        events.append({"action": action, "status": "failed", "error": str(exc)})
        raise
    finally:
        log_path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        cdp.close()


def prepare(port: int, expected_url=None) -> tuple[CDP, str]:
    cdp = CDP(port, expected_url) if expected_url else CDP(port)
    _wait_for_page(cdp, expected_url=expected_url)
    return cdp, viewport_rect(cdp)
