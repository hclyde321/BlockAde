#!/bin/zsh
cd -- "${0:A:h}" || exit 1
export PATH="$PWD/.tools/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ ! -x .venv/bin/python ]]; then
  print '找不到專案 Python 環境，請先安裝 .venv 與 requirements.txt。'
  read '?按 Enter 關閉視窗。'
  exit 1
fi
if .venv/bin/python -c 'import json,urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=2)).get("ok") is True' 2>/dev/null; then
  open 'http://127.0.0.1:8000'
  exit 0
fi
( sleep 2; open 'http://127.0.0.1:8000' ) &
print '使用 App 期間請保留此視窗；按 Control+C 可停止服務。'
.venv/bin/python -u server.py
read '?服務已停止。按 Enter 關閉視窗。'
