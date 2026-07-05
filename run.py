"""사진 정리 앱 런처: uvicorn 기동 + 브라우저 열기.

사용: py -3.13 run.py [--port 8777] [--no-browser] [--persist]
이미 실행 중이면(포트 점유 + health 응답) 새로 띄우지 않고 브라우저만 연다.
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return "root" in json.load(r)
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--persist", action="store_true",
                    help="브라우저를 닫아도 서버 유지 (개발용)")
    args = ap.parse_args()

    if already_running(args.port):
        print(f"이미 실행 중 → 브라우저만 엽니다 (http://127.0.0.1:{args.port}/)")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        return

    if args.persist or args.no_browser:
        os.environ["PHOTO_APP_PERSIST"] = "1"  # 개발 모드: 자동종료 끔

    if not args.no_browser:
        threading.Thread(
            target=lambda: (time.sleep(1.5), webbrowser.open(f"http://127.0.0.1:{args.port}/")),
            daemon=True,
        ).start()

    import uvicorn

    from backend.main import app  # frozen(PyInstaller) 빌드에서도 정적 분석되도록 직접 import

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
