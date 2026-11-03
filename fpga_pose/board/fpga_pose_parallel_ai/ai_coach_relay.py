"""Run on the Internet-connected PC; fetch only requested summaries from PYNQ.

No listening port, browser CORS, pip packages, or cloud key on the board needed.
"""
import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from ai_coach import DeepSeekClient, MAX_RESPONSE_BYTES, _NoRedirect, read_bounded


def board_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("--board 必须为板端根地址，例如 http://192.168.2.99:8081")
    return value.rstrip("/")


def post_json(base, path, payload, timeout=8.0):
    request = Request(base + path, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        deadline = time.monotonic() + timeout
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = read_bounded(response, MAX_RESPONSE_BYTES, deadline)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("板端响应过大")
        result = json.loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("板端响应格式无效")
        return result
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise ValueError("板端 HTTP %d；请核对地址、服务和配对令牌" % code) from None
    except (URLError, OSError, TimeoutError):
        raise ValueError("无法连接板端；请检查电脑与 PYNQ 的网络") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("板端未返回有效 JSON") from None


def run_once(base, token, client, post=post_json):
    response = post(base, "/api/ai/relay/poll", {"token": token})
    job = response.get("job")
    if not job:
        return "idle"
    if not isinstance(job, dict) or not isinstance(job.get("job_id"), str) or "telemetry" not in job:
        raise ValueError("板端 AI 任务格式无效")
    payload = {"token": token, "job_id": job["job_id"]}
    # No retries: one admitted board job produces at most one paid API call.
    try:
        if job.get("remaining_seconds", 0) < client.timeout + 2:
            payload["error"] = "insufficient_time"
        else:
            payload["feedback"] = client.evaluate(job["telemetry"])
    except Exception:
        # Provider errors may contain response data; send only a fixed code.
        payload["error"] = "provider_failed"
    acknowledgement = post(base, "/api/ai/relay/result", payload)
    if not acknowledgement.get("accepted"):
        return "stale"
    return "error" if payload.get("error") else "complete"


def main(argv=None):
    parser = argparse.ArgumentParser(description="PYNQ DeepSeek 电脑助手（仅轮询，不开放监听端口）")
    parser.add_argument("--board", default="http://192.168.2.99:8081")
    parser.add_argument("--token", default=os.environ.get("AI_COACH_PAIR_TOKEN"),
                        help="Notebook 显示的配对令牌，也可用 AI_COACH_PAIR_TOKEN 环境变量")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="仅轮询一次，用于检查连接")
    args = parser.parse_args(argv)
    try:
        base = board_url(args.board)
        if not args.token or len(args.token) > 128:
            raise ValueError("请设置 AI_COACH_PAIR_TOKEN 或 --token；令牌从当前 Notebook 取得")
        client = DeepSeekClient(model=args.model, timeout=args.timeout)
        if not client.configured:
            raise ValueError("请先在电脑设置 DEEPSEEK_API_KEY 环境变量")
    except (ValueError, OverflowError):
        # Avoid argparse error echoing a command containing credentials.
        print("配置错误：需要板端根地址、当前配对令牌及电脑 DEEPSEEK_API_KEY。", file=sys.stderr)
        return 2
    print("DeepSeek 电脑助手已启动。保持窗口开启，网页启用 AI 后才会上传统计。", flush=True)
    print("按 Ctrl+C 退出；API 密钥和配对令牌不会显示。", flush=True)
    previous = None
    try:
        while True:
            try:
                state = run_once(base, args.token, client)
                if state == "complete":
                    print("AI 反馈已返回板端。", flush=True)
                elif state == "error":
                    print("AI 评估失败，请核对电脑网络、账户额度和模型名称。", flush=True)
                elif state == "stale":
                    print("监测会话已变化，旧反馈已丢弃。", flush=True)
                elif previous != "idle":
                    print("已连接板端，等待网页发起 AI 评估。", flush=True)
            except ValueError:
                state = "connection_error"
                if previous != state:
                    print("板端暂不可用或配对失败；请核对地址、Notebook 服务和令牌。", flush=True)
            previous = state
            if args.once:
                return 1 if state in ("connection_error", "error") else 0
            time.sleep(max(0.5, min(10.0, args.interval)))
    except KeyboardInterrupt:
        print("电脑助手已退出，实时检测继续运行。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
