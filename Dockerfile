FROM python:3.13-slim

WORKDIR /app

# 依存関係を先にコピーしてビルドキャッシュを効かせる
COPY btc_trading_tool/requirements.txt ./btc_trading_tool/requirements.txt
COPY ai_review/requirements.txt ./ai_review/requirements.txt
RUN pip install --no-cache-dir -r btc_trading_tool/requirements.txt \
    && pip install --no-cache-dir -r ai_review/requirements.txt

# アプリ本体をコピー
COPY . .

# 既定のコマンド（docker-compose.yml側で上書きする想定）
CMD ["python", "--version"]
