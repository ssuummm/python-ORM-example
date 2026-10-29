#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
외부망 AI 연동 가능 여부 점검 스크립트 (API / 원격 MCP / Skill)
=================================================================

사내망에서 아래 세 가지가 실제로 동작하는지 단계별로 점검한다.

  [API]   공개 날씨 API (Open-Meteo, 인증 불필요, 읽기 전용)
  [MCP]   공개 원격 MCP 서버 (Streamable HTTP, 인증 불필요, 읽기 전용 문서 조회)
  [SKILL] Agent Skill
          - SKILL-1: 공개 Skill 저장소(GitHub anthropics/skills)에서 SKILL.md 내려받기
          - SKILL-2: Claude API로 Skill 실행 (ANTHROPIC_API_KEY 가 있을 때만)

각 대상마다 DNS → TCP(직접) → HTTP(프록시 설정 반영) 순서로 확인하고,
실패하면 원인을 추정해서 보여준다 (DNS 차단 / 방화벽 Drop / 프록시 차단 / SSL 검사 등).

특징
  - Python 표준 라이브러리만 사용 (pip 설치 불필요, Python 3.8+)
  - HTTPS_PROXY / HTTP_PROXY / NO_PROXY 환경변수를 그대로 따름
  - 사내 SSL 검사(TLS Inspection)용 CA 인증서는 --ca-bundle 로 지정
  - 사내 데이터는 전송하지 않음. 고정된 공개 질의(서울 날씨, 공개 문서 검색 등)만 보냄

사용 예
  python external_access_check.py
  python external_access_check.py --only api,mcp
  python external_access_check.py --ca-bundle C:\\certs\\corp-root.pem
  python external_access_check.py --report result.json
  (Skill 실행까지 점검) set ANTHROPIC_API_KEY=sk-ant-...  후 실행
"""

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# 점검 대상 정의 (필요하면 여기만 수정)
# ---------------------------------------------------------------------------

API_TARGETS = [
    {
        "name": "Open-Meteo 날씨 예보 (서울 현재 기온)",
        "url": "https://api.open-meteo.com/v1/forecast"
               "?latitude=37.5665&longitude=126.9780"
               "&current=temperature_2m,wind_speed_10m&timezone=Asia%2FSeoul",
        "summarize": lambda d: "서울 현재 기온 {}°C, 풍속 {} km/h (관측시각 {})".format(
            d["current"]["temperature_2m"], d["current"]["wind_speed_10m"], d["current"]["time"]),
    },
    {
        "name": "Open-Meteo 지오코딩 (Busan 검색)",
        "url": "https://geocoding-api.open-meteo.com/v1/search?name=Busan&count=1&language=ko",
        "summarize": lambda d: "{} (위도 {}, 경도 {})".format(
            d["results"][0]["name"], d["results"][0]["latitude"], d["results"][0]["longitude"]),
    },
]

# 운영 주체가 명확하고, 인증 없이 '공개 문서 조회'만 하는 원격 MCP 서버를 사용한다.
# (공개 날씨 MCP는 대부분 개인이 운영하는 서버라 보안 점검용으로는 제외)
MCP_TARGETS = [
    {
        "name": "DeepWiki MCP (Cognition, 공개 GitHub 저장소 문서)",
        "url": "https://mcp.deepwiki.com/mcp",
        "tool": "read_wiki_structure",
        "arguments": {"repoName": "modelcontextprotocol/python-sdk"},
    },
    {
        "name": "Microsoft Learn MCP (Microsoft 공식 문서 검색)",
        "url": "https://learn.microsoft.com/api/mcp",
        "tool": "microsoft_docs_search",
        "arguments": {"query": "Azure Functions overview"},
    },
]

SKILL_PUBLIC_URL = "https://raw.githubusercontent.com/anthropics/skills/main/skills/xlsx/SKILL.md"

ANTHROPIC_API = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"   # 환경변수 ANTHROPIC_MODEL 로 변경 가능

MCP_PROTOCOL_VERSION = "2025-06-18"
USER_AGENT = "external-access-check/1.0 (python-urllib)"


# ---------------------------------------------------------------------------
# 공통 유틸
# ---------------------------------------------------------------------------

class Ctx:
    timeout = 15
    ssl_ctx = None
    verbose = False


def c(text, color):
    if not sys.stdout.isatty() or os.name == "nt" and "WT_SESSION" not in os.environ:
        return text
    codes = {"g": "32", "r": "31", "y": "33", "b": "36", "dim": "2"}
    return "\033[{}m{}\033[0m".format(codes[color], text)


def log(msg=""):
    print(msg, flush=True)


def detail(msg):
    log("      " + c(msg, "dim"))


def get_proxy_for(url):
    proxies = urllib.request.getproxies()
    host = urllib.parse.urlparse(url).hostname or ""
    if urllib.request.proxy_bypass(host):
        return None
    return proxies.get(urllib.parse.urlparse(url).scheme)


def check_dns(host):
    t = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        return True, "{} ({}ms)".format(", ".join(ips[:3]), int((time.time() - t) * 1000))
    except socket.gaierror as e:
        return False, str(e)


def check_tcp(host, port=443):
    t = time.time()
    try:
        with socket.create_connection((host, port), timeout=min(Ctx.timeout, 8)):
            return True, "{}ms".format(int((time.time() - t) * 1000))
    except socket.timeout:
        return False, "timeout (방화벽에서 Drop 가능성)"
    except OSError as e:
        return False, str(e)


def http(method, url, headers=None, body=None):
    """urllib 요청. (status, headers, body_bytes) 또는 예외를 그대로 올린다."""
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=Ctx.ssl_ctx))
    resp = opener.open(req, timeout=Ctx.timeout)
    return resp


def diagnose(exc):
    """예외를 사람이 읽을 수 있는 원인 추정으로 바꾼다."""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        try:
            snippet = exc.read(300).decode("utf-8", "replace").replace("\n", " ")
        except Exception:
            snippet = ""
        hint = {
            401: "인증 실패 (API 키 확인)",
            403: "접근 거부 - 사내 프록시/보안장비 차단 페이지일 가능성",
            407: "프록시 인증 필요 - HTTPS_PROXY=http://user:pass@proxy:port 형식 확인",
            404: "경로 없음 (엔드포인트 변경 가능성)",
            405: "허용되지 않은 메서드 - 프록시가 POST를 막았을 가능성",
            429: "요청 한도 초과 (잠시 후 재시도)",
        }.get(code, "서버/프록시 오류")
        return "HTTP {} : {} | {}".format(code, hint, snippet[:160])
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return ("SSL 인증서 검증 실패 - 사내 SSL 검사(TLS Inspection) 가능성. "
                "보안팀 루트 CA를 --ca-bundle 로 지정해 재시도")
    if isinstance(reason, ssl.SSLError):
        return "TLS 협상 실패 - 보안장비가 TLS를 끊었을 가능성 ({})".format(text)
    if isinstance(reason, socket.gaierror):
        return "DNS 조회 실패 - 외부 도메인 해석 차단 또는 프록시 경유 필요"
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in text:
        return "타임아웃 - 방화벽 Drop 또는 프록시 미설정"
    if "Connection refused" in text:
        return "연결 거부 - 방화벽 Reject 또는 프록시 주소 오류"
    if "Tunnel connection failed" in text:
        return "프록시 CONNECT 거부 - 프록시 정책에서 해당 도메인 차단 ({})".format(text)
    if "reset" in text.lower():
        return "연결 리셋 - 보안장비(IPS/DLP)가 세션을 끊었을 가능성"
    return "{}: {}".format(type(reason).__name__, text)


class Result:
    def __init__(self, category, name, url):
        self.category, self.name, self.url = category, name, url
        self.status = "FAIL"
        self.steps = {}
        self.message = ""
        self.elapsed_ms = 0

    def to_dict(self):
        return self.__dict__.copy()


def network_precheck(res, url):
    """DNS, 직접 TCP 연결 확인 (프록시 환경에서는 TCP 실패가 정상일 수 있음)."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    proxy = get_proxy_for(url)
    ok, msg = check_dns(host)
    res.steps["dns"] = ("OK " if ok else "FAIL ") + msg
    detail("DNS   : " + res.steps["dns"])
    if proxy:
        res.steps["proxy"] = proxy
        detail("PROXY : " + proxy + " 경유 (직접 TCP 점검 생략)")
    else:
        ok_t, msg_t = check_tcp(host, port)
        res.steps["tcp_direct"] = ("OK " if ok_t else "FAIL ") + msg_t
        detail("TCP   : " + res.steps["tcp_direct"])


def print_result(res):
    tag = {"PASS": c("[PASS]", "g"), "FAIL": c("[FAIL]", "r"), "SKIP": c("[SKIP]", "y")}[res.status]
    log("   {} {}".format(tag, res.message))
    log()


# ---------------------------------------------------------------------------
# 1. 공개 API 점검
# ---------------------------------------------------------------------------

def test_api(t):
    res = Result("API", t["name"], t["url"])
    log(c(" ▶ [API] " + t["name"], "b"))
    detail(t["url"])
    network_precheck(res, t["url"])
    start = time.time()
    try:
        resp = http("GET", t["url"], {"Accept": "application/json"})
        data = json.loads(resp.read().decode("utf-8"))
        res.status = "PASS"
        res.message = "HTTP {} | {}".format(resp.status, t["summarize"](data))
    except Exception as e:
        res.message = diagnose(e)
    res.elapsed_ms = int((time.time() - start) * 1000)
    print_result(res)
    return res


# ---------------------------------------------------------------------------
# 2. 원격 MCP 점검 (Streamable HTTP, JSON-RPC 2.0 직접 구현)
# ---------------------------------------------------------------------------

class McpClient:
    def __init__(self, url):
        self.url = url
        self.session_id = None
        self.protocol = MCP_PROTOCOL_VERSION
        self._id = 0

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _post(self, payload):
        resp = http("POST", self.url, self._headers(), payload)
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        return resp

    @staticmethod
    def _read_message(resp, want_id):
        """응답이 JSON이든 SSE(text/event-stream)든 want_id 에 해당하는 메시지를 꺼낸다."""
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ctype:
            buf = []
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    buf.append(line[5:].lstrip())
                elif line == "" and buf:
                    msg = json.loads("\n".join(buf))
                    buf = []
                    if isinstance(msg, dict) and msg.get("id") == want_id:
                        return msg
            if buf:
                msg = json.loads("\n".join(buf))
                if msg.get("id") == want_id:
                    return msg
            raise RuntimeError("SSE 스트림에서 응답(id={})을 찾지 못함".format(want_id))
        msg = json.loads(resp.read().decode("utf-8"))
        if isinstance(msg, list):
            msg = next(m for m in msg if m.get("id") == want_id)
        return msg

    def request(self, method, params=None):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        msg = self._read_message(self._post(payload), self._id)
        if "error" in msg:
            raise RuntimeError("JSON-RPC error {}: {}".format(
                msg["error"].get("code"), msg["error"].get("message")))
        return msg["result"]

    def notify(self, method):
        resp = self._post({"jsonrpc": "2.0", "method": method})
        resp.read()

    def close(self):
        if not self.session_id:
            return
        try:
            http("DELETE", self.url, self._headers()).read()
        except Exception:
            pass   # 세션 종료를 지원하지 않는 서버도 있음


def test_mcp(t):
    res = Result("MCP", t["name"], t["url"])
    log(c(" ▶ [MCP] " + t["name"], "b"))
    detail(t["url"])
    network_precheck(res, t["url"])
    start = time.time()
    cli = McpClient(t["url"])
    try:
        init = cli.request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "external-access-check", "version": "1.0"},
        })
        cli.protocol = init.get("protocolVersion", MCP_PROTOCOL_VERSION)
        srv = init.get("serverInfo", {})
        res.steps["initialize"] = "OK {} {} (protocol {})".format(
            srv.get("name"), srv.get("version", ""), cli.protocol)
        detail("init  : " + res.steps["initialize"])
        cli.notify("notifications/initialized")

        tools = cli.request("tools/list", {}).get("tools", [])
        names = [x["name"] for x in tools]
        res.steps["tools/list"] = "OK {}개: {}".format(len(names), ", ".join(names[:6]))
        detail("tools : " + res.steps["tools/list"])

        if t["tool"] in names:
            out = cli.request("tools/call", {"name": t["tool"], "arguments": t["arguments"]})
            text = " ".join(
                blk.get("text", "") for blk in out.get("content", []) if blk.get("type") == "text")
            preview = " ".join(text.split())[:120]
            if out.get("isError"):
                res.message = "도구 호출 오류 응답: " + preview
            else:
                res.status = "PASS"
                res.message = "initialize → tools/list → tools/call({}) 성공 | {}...".format(
                    t["tool"], preview)
        else:
            res.status = "PASS"
            res.message = "연결·목록 조회 성공 (도구 '{}' 없음 → 호출 생략)".format(t["tool"])
    except Exception as e:
        res.message = diagnose(e) if isinstance(e, (urllib.error.URLError, OSError)) else str(e)
    finally:
        cli.close()
    res.elapsed_ms = int((time.time() - start) * 1000)
    print_result(res)
    return res


# ---------------------------------------------------------------------------
# 3. Skill 점검
# ---------------------------------------------------------------------------

def parse_frontmatter(md):
    if not md.startswith("---"):
        return {}
    block = md.split("---", 2)[1]
    meta = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta


def test_skill_public():
    res = Result("SKILL", "공개 Skill 다운로드 (GitHub anthropics/skills · xlsx)", SKILL_PUBLIC_URL)
    log(c(" ▶ [SKILL-1] " + res.name, "b"))
    detail(SKILL_PUBLIC_URL)
    network_precheck(res, SKILL_PUBLIC_URL)
    start = time.time()
    try:
        md = http("GET", SKILL_PUBLIC_URL).read().decode("utf-8")
        meta = parse_frontmatter(md)
        if meta.get("name"):
            res.status = "PASS"
            res.message = "SKILL.md {}바이트 수신 | name={} | description={}...".format(
                len(md), meta.get("name"), meta.get("description", "")[:60])
        else:
            res.message = "응답은 받았으나 SKILL.md 형식이 아님 (차단 페이지로 대체됐을 가능성)"
    except Exception as e:
        res.message = diagnose(e)
    res.elapsed_ms = int((time.time() - start) * 1000)
    print_result(res)
    return res


def test_skill_api():
    """Claude API에서 Anthropic 제공 xlsx Skill을 실제로 실행한다 (API 키 필요, 소량 과금 발생)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    res = Result("SKILL", "Claude API Skill 실행 (Anthropic xlsx Skill)", ANTHROPIC_API)
    log(c(" ▶ [SKILL-2] " + res.name, "b"))
    detail(ANTHROPIC_API + "/v1/skills , /v1/messages  (model={})".format(model))
    network_precheck(res, ANTHROPIC_API)

    if not key:
        # 키가 없어도 엔드포인트 도달 여부는 확인할 수 있다 (401이면 '도달 가능')
        try:
            http("GET", ANTHROPIC_API + "/v1/models",
                 {"anthropic-version": ANTHROPIC_VERSION}).read()
            res.message = "예상치 못한 응답"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                res.status = "SKIP"
                res.message = ("api.anthropic.com 도달 가능 (401 인증 필요 응답 확인). "
                               "ANTHROPIC_API_KEY 설정 시 Skill 실행까지 점검")
            else:
                res.message = diagnose(e)
        except Exception as e:
            res.message = diagnose(e)
        print_result(res)
        return res

    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    start = time.time()
    try:
        # (1) 사용 가능한 Skill 목록 조회
        sk = json.loads(http("GET", ANTHROPIC_API + "/v1/skills?limit=20", headers).read())
        ids = [s.get("id") for s in sk.get("data", [])]
        res.steps["skills_list"] = "OK {}개: {}".format(len(ids), ", ".join(map(str, ids[:6])))
        detail("skills: " + res.steps["skills_list"])

        # (2) xlsx Skill로 아주 작은 파일 생성 요청 (사내 데이터 미포함)
        body = {
            "model": model,
            "max_tokens": 1024,
            "container": {"skills": [{"type": "anthropic", "skill_id": "xlsx", "version": "latest"}]},
            "tools": [{"type": "code_execution_20250825", "name": "code_execution"}],
            "messages": [{"role": "user", "content":
                          "Using the xlsx skill, create connectivity_test.xlsx with A1='hello' "
                          "and B1=1+1 as a formula. Reply with one short sentence."}],
        }
        msg = json.loads(http("POST", ANTHROPIC_API + "/v1/messages", headers, body).read())
        types = [b.get("type") for b in msg.get("content", [])]
        file_ids = []
        for b in msg.get("content", []):
            inner = b.get("content")
            if isinstance(inner, dict):
                for f in inner.get("content", []) or []:
                    if isinstance(f, dict) and f.get("file_id"):
                        file_ids.append(f["file_id"])
        text = " ".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")
        res.steps["messages"] = "stop_reason={} | blocks={}".format(msg.get("stop_reason"), types)
        detail("run   : " + res.steps["messages"])
        ran_code = any("code_execution" in (t or "") for t in types)
        res.status = "PASS" if ran_code else "FAIL"
        res.message = "Skill 실행 {} | 생성 파일 {} | {}".format(
            "확인" if ran_code else "미확인(코드 실행 블록 없음)",
            file_ids or "없음", " ".join(text.split())[:100])
    except Exception as e:
        res.message = diagnose(e)
    res.elapsed_ms = int((time.time() - start) * 1000)
    print_result(res)
    return res


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="외부망 API / MCP / Skill 연결 점검")
    ap.add_argument("--only", default="api,mcp,skill", help="점검 범위 (예: api,mcp)")
    ap.add_argument("--timeout", type=int, default=15, help="요청 타임아웃(초)")
    ap.add_argument("--ca-bundle", help="사내 SSL 검사용 루트 CA(PEM) 경로")
    ap.add_argument("--report", help="결과를 JSON 파일로 저장")
    args = ap.parse_args()

    Ctx.timeout = args.timeout
    # 시스템 기본 CA는 유지하고 사내 CA만 추가로 신뢰한다.
    # (cafile= 로 넘기면 기본 CA가 빠져서, 복호화 예외 도메인이 오히려 실패한다)
    Ctx.ssl_ctx = ssl.create_default_context()
    if args.ca_bundle:
        Ctx.ssl_ctx.load_verify_locations(cafile=args.ca_bundle)
    scope = {s.strip().lower() for s in args.only.split(",")}

    log("=" * 72)
    log(" 외부망 AI 연동 점검  |  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    log(" host={}  python={}".format(socket.gethostname(), sys.version.split()[0]))
    px = urllib.request.getproxies()
    log(" proxy={}".format({k: v for k, v in px.items() if k in ("http", "https", "no")} or "없음"))
    log(" ca-bundle={}".format(args.ca_bundle or "시스템 기본"))
    log("=" * 72)
    log()

    results = []
    if "api" in scope:
        results += [test_api(t) for t in API_TARGETS]
    if "mcp" in scope:
        results += [test_mcp(t) for t in MCP_TARGETS]
    if "skill" in scope:
        results.append(test_skill_public())
        results.append(test_skill_api())

    log("=" * 72)
    log(" 요약")
    log("-" * 72)
    for r in results:
        log(" {:<5} {:<6} {}".format(r.status, r.category, r.name))
    log("=" * 72)
    passed = sum(r.status == "PASS" for r in results)
    log(" PASS {} / FAIL {} / SKIP {}".format(
        passed, sum(r.status == "FAIL" for r in results), sum(r.status == "SKIP" for r in results)))

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({
                "checked_at": datetime.now().isoformat(),
                "host": socket.gethostname(),
                "proxy": px,
                "results": [r.to_dict() for r in results],
            }, f, ensure_ascii=False, indent=2)
        log(" 결과 저장: " + args.report)

    sys.exit(0 if all(r.status != "FAIL" for r in results) else 1)


if __name__ == "__main__":
    main()
