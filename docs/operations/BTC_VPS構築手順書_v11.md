# BTC/JPY 自動売買システム VPS構築手順書（実践版）

作成: 2026-07-17（仕様書v35時点の情報に基づく）
最終更新: 2026-08-02（v11。2026-08-01のVPS実機構築（ConoHa）で実際に発生した
つまずきをすべて反映し、全面的に書き直した。知識がなくても上から順にコピー＆
ペーストしていけば環境が整う粒度を目指している。旧版（v10以前）は設計方針・
経緯の記録として9.5節「変更履歴」に要約を残す）
対象読者: Takuya本人（VPS実機構築時に上から順に実行する）

---

## この手順書の使い方

- 上から順番に実行すれば、VPS契約からGMOコインでの実資金運用開始まで一通り完了する
- コマンドはすべて**コピー＆ペーストして使える形**にしてある。`<...>`で囲まれた部分だけは自分の環境の値に置き換える
- 実際に構築中つまずいた箇所には「⚠️ 実際にここでつまずいた」という注記を付けている。ここは特に注意して読む
- 全体を通して、**VPSプロバイダはConoHa、OSはUbuntu（24.04系、socket activation方式）を前提**にしている。別プロバイダ・別OSの場合は、8章のトラブルシューティング集で分岐点を確認すること

---

## 0. 変数の設定（最初に1回だけ）

このあと出てくるコマンドは、以下の変数を使う形で書いてある。**実際の値が決まったら、その都度ここに書き足しておくと、以降のコピペで迷わない。**

**手元のWindows PC（PowerShell）側**

```powershell
# VPSのIPアドレス（ConoHaのサーバー詳細画面で確認）
$VPS_IP = "<VPSのIPアドレス>"

# SSHのポート番号（変更していなければ22。この手順書では2222への変更を例にしている）
$SSH_PORT = "2222"

# VPS上の作業用ユーザー名
$VPS_USER = "takuya"

# 秘密鍵ファイルのパス（ConoHaでサーバー作成時にダウンロードしたもの）
$PEM = "C:\Users\<Windowsのユーザー名>\.ssh\<鍵ファイル名>.pem"

# プロジェクトのローカルリポジトリのパス
$LOCAL_REPO = "C:\Users\<Windowsのユーザー名>\Cursor\Projects\gmo-btc-ws"
```

**VPS側（Linux、以降"VPS側"と書いてあるコマンドはここで実行）**

```bash
# プロジェクトの配置パス
PROJECT_ROOT=/opt/gmo-btc-ws
```

以降、手順書内の`$VPS_IP`等はこの変数がそのまま展開される想定で書いている（PowerShell側）。VPS側のコマンドは基本的に`/opt/gmo-btc-ws`を直接書いている。

---

## 1. VPS契約・初期セットアップ

### 1.1 サーバー作成（ConoHaの場合）

- [ ] ConoHaの管理画面からサーバーを新規作成する
  - OSイメージ: Ubuntu 24.04（またはUbuntu 22.04）LTS
  - スペック目安: メモリ2GB以上（Docker + 3コンテナ想定）、ディスク20GB以上
  - SSH Key: 事前に手元PCで作成した公開鍵を登録しておく（後述1.2でこの鍵を使う）
- [ ] サーバー作成完了後、管理画面の「サーバー詳細」でIPアドレスを確認し、0章の`$VPS_IP`に設定する

### 1.2 ⚠️ ConoHaのセキュリティグループを必ず確認する（実際にここで数時間つまずいた）

**ConoHaは新規サーバーのセキュリティグループが`default`のままだと、SSHを含む外部からの通信が全て遮断される。** これに気づかず「SSH接続できない」で長時間つまずくケースが多いので、最初に必ず確認する。

1. ConoHa管理画面 → サーバー一覧 → 対象サーバーを開く
2. **「ネットワーク情報」タブ**を開く（「セキュリティグループの管理」という別画面と混同しないこと。管理画面でルールを作成しても、ここで実際にサーバーへ割り当てないと反映されない）
3. 「セキュリティグループ」欄が`default`のままになっていないか確認する
4. 22番ポート用に用意されている`IPv4v6-SSH`を選択・保存する（後述1.8でポートを変更する場合は、その時点で専用グループを追加作成する）

### 1.3 初回SSHログイン（rootユーザー、一時的）

手元PCのPowerShellから、初回はrootユーザー・22番ポート・ConoHa発行の秘密鍵で接続する。

```powershell
ssh -i $PEM root@$VPS_IP
```

初回接続時、以下のようなホスト鍵確認メッセージが出る。これは「このサーバーに接続するのは初めてです」という確認なので、`yes`と答えてよい。

```
This key is not known by any other names.
Are you sure you want to continue connecting (yes/no/[fingerprint])? yes
```

**ここで`Connection timed out`になった場合** → 1.2節のセキュリティグループ設定を再確認する（0章「困ったときの調べ方」も参照）。

### 1.4 作業用ユーザーを作成する（VPS側、rootで実行）

rootを日常作業に使い続けるのは避け、専用の作業用ユーザーを作る。

```bash
adduser takuya
```

途中で以下のようなプロンプトが複数出るが、**すべてEnterキーでスキップして問題ない**（パスワードだけは入力する。それ以外のFull Name等は運用に一切影響しない管理用メモ欄）。

```
Enter the new value, or press ENTER for the default
        Full Name []:
```

最後に`Is the information correct? [Y/n]`と聞かれたら`Y`（またはEnter）で確定する。

```bash
usermod -aG sudo takuya
```

### 1.5 作業用ユーザーに公開鍵を登録する

**⚠️ 実際にここで一番つまずいた。** 手順は以下の順番が確実。

**方法A（推奨・確実）: 秘密鍵から公開鍵を直接導出して貼り付ける**

ConoHaがrootに登録した公開鍵は、`takuya`ユーザー作成時には自動的には引き継がれない。また、`root`の`authorized_keys`自体が空（0バイト）になっているケースもある（ConoHa側の内部的な鍵管理の都合と思われる）。そのため、確実なのは**手元PCの秘密鍵から公開鍵を直接取り出して登録する方法**。

手元PCのPowerShellで、秘密鍵から公開鍵を導出する。

```powershell
ssh-keygen -y -f $PEM
```

`ssh-ed25519 AAAA...`（または`ssh-rsa AAAA...`）から始まる1行が表示される。**この1行をまるごとコピーする。**

VPS側（rootのまま）で、`takuya`の`.ssh`ディレクトリを作成し、その公開鍵を書き込む。

```bash
mkdir -p /home/takuya/.ssh
tee -a /home/takuya/.ssh/authorized_keys > /dev/null << 'EOF'
（ここに手元PCでコピーした1行を貼り付け）
EOF
chown -R takuya:takuya /home/takuya/.ssh
chmod 700 /home/takuya/.ssh
chmod 600 /home/takuya/.ssh/authorized_keys
```

**⚠️ PowerShellの`cat`パイプでの登録は失敗しやすいので避ける。** `cat ~/.ssh/id_ed25519.pub | ssh ... "... >> authorized_keys"`のようなワンライナーは、PowerShellの`cat`（`Get-Content`のエイリアス）がBOM付与・改行コード変換を行うことがあり、SSH側で鍵として認識されないことがある。上記の「`ssh-keygen -y`で表示された1行をコピー＆ペースト」する方法が確実。

**確認: `authorized_keys`が存在しない場合**

もし`cat ~/.ssh/authorized_keys`で「No such file or directory」と出る場合は、以下を先に実行してから公開鍵を貼り付ける。

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### 1.6 鍵認証での接続を確認する

手元PCから、`takuya`ユーザー・22番ポートで接続できるか確認する（ポート変更はまだ行っていないので、この時点では22番のまま）。

```powershell
ssh -i $PEM takuya@$VPS_IP
```

**繋がらない場合の切り分け**（8章のトラブルシューティング集も参照）
- `Permission denied (publickey)` → 1.5節の公開鍵登録をやり直す。`sudo cat /home/takuya/.ssh/authorized_keys`で中身が本当に入っているか、`ls -la /home/takuya/.ssh`で所有者・パーミッションが正しいか確認する
- `sudo journalctl -u ssh -n 30 --no-pager`で、sshd側が拒否した具体的な理由を確認できる

### 1.7 パスワード認証を無効化する

鍵認証で接続できることを確認できたら、パスワード認証を無効化する。

```bash
sudo nano /etc/ssh/sshd_config
```
（ブラウザコンソールで`nano`のショートカットが効かない場合は、8.9節の対策を参照。または以下のように`sed`で直接書き換えてもよい）

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

**⚠️ 変更したのに実効値が`yes`のまま、ということが起こりうる。** `sshd_config`本体を`no`に変更しても、`/etc/ssh/sshd_config.d/50-cloud-init.conf`（VPSプロバイダのcloud-initが自動生成する設定）に`PasswordAuthentication yes`が書かれていて、そちらが`Include`の読み込み順序の都合で優先されてしまうことがある（sshdは「最初にマッチした設定が有効」というルール）。

**必ず「実際に有効な値」を確認する。**

```bash
sudo sshd -T | grep passwordauthentication
```

`no`と出ていなければ、以下で`50-cloud-init.conf`側も確認・修正する。

```bash
sudo cat /etc/ssh/sshd_config.d/50-cloud-init.conf
sudo sed -i 's/^PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config.d/50-cloud-init.conf
sudo systemctl restart ssh
sudo sshd -T | grep passwordauthentication
```

**確認は、今のセッションを切断せず、別ウィンドウで新しいSSHセッションを開いて行うこと。** 万一設定を誤ってロックアウトされても、今のセッションが残っていれば復旧できる。

### 1.8 （任意）SSHポートを変更する

デフォルトの22番以外を使いたい場合（ここでは2222番を例にする）は、以下の手順が必要。**Ubuntu 22.04/24.04系は、`sshd_config`の`Port`変更だけでは反映されないので要注意。**

**1. ConoHa側で、2222番用のセキュリティグループを新規作成し、サーバーに割り当てる**（1.2節と同じ手順で、ポート番号だけ2222にしたグループを追加作成する）

**2. サービス名の確認（Ubuntu 24.04では`sshd`ではなく`ssh`）**

```bash
sudo systemctl status ssh
```
（`sudo systemctl restart sshd`のように`sshd`という名前で実行すると`Unit sshd.service not found.`になる。Ubuntu 24.04のユニット名は`ssh`）

**3. ソケットアクティベーションの確認**

上記コマンドの出力に`TriggeredBy: ● ssh.socket`という表示があれば、Ubuntu 22.04以降のソケットアクティベーション方式で動いている。この場合、`sshd_config`の`Port`をいくら変更しても実際の待受ポートには反映されないため、`ssh.socket`側に上書き設定を追加する。

```bash
sudo mkdir -p /etc/systemd/system/ssh.socket.d

sudo tee /etc/systemd/system/ssh.socket.d/listen.conf > /dev/null << 'EOF'
[Socket]
ListenStream=
ListenStream=0.0.0.0:2222
ListenStream=[::]:2222
EOF

sudo systemctl daemon-reload
sudo systemctl restart ssh.socket
```

**ポイント2つ**
- 先頭の`ListenStream=`（値なし）で、デフォルトの22番設定を一度クリアする。これをしないと22番・2222番の両方で待ち受け続けてしまうことがある
- **IPv4（`0.0.0.0:2222`）とIPv6（`[::]:2222`）を両方明示すること。** `ListenStream=2222`とだけ書くと、環境によってはIPv6専用ソケットにしかならず、IPv4アドレスからの接続だけ`Connection refused`になる（実際にこれで一度ハマった）

**4. 反映確認**

```bash
sudo ss -tlnp | grep ssh
```

`0.0.0.0:2222`と`[::]:2222`の**2行**が出ていればOK。片方しか出ていない場合は上記のIPv4/IPv6明示指定を見直す。

**5. 手元PCから新ポートで接続確認**

```powershell
ssh -p $SSH_PORT -i $PEM takuya@$VPS_IP
```

これが通ることを確認できてから、以降のコマンドではすべて`-p $SSH_PORT`（VPS側の作業では22番から変わったことを意識）を付ける。**ポート変更後は、SSH関連のすべてのコマンドにポート指定を付け忘れないこと。**

### 1.9 ファイアウォール設定

ダッシュボードはSSHトンネル経由でのみアクセスする方針のため、外部に公開が必要なポートはSSH（1.8節で変更した場合はそのポート）のみ。

```bash
sudo ufw allow 2222/tcp
sudo ufw enable
sudo ufw status
```

**注意**: `ufw enable`前に必ずSSH許可ルールを先に追加すること（順序を間違えるとSSH接続そのものが切断される）。22番のまま使っている場合は`sudo ufw allow OpenSSH`でよい。

### 1.10 OSアップデート

```bash
sudo apt update && sudo apt upgrade -y
```

---

## 2. Docker Engineのインストール

```bash
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.gpg
sudo install -m 0755 -d /etc/apt/keyrings
sudo mv /tmp/docker.gpg /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

- [ ] 作業用ユーザーを`docker`グループに追加する

```bash
sudo usermod -aG docker $USER
```

**⚠️ グループ追加は、今のセッションには反映されない。** 一度セッションを完全に終了し（`exit`）、再接続してから確認する。

```bash
exit
```
（再接続後）
```bash
groups
```
`docker`が含まれていることを確認できてから次に進む。含まれていなければ、`sudo usermod -aG docker $USER`を再実行し、もう一度ログアウト・再ログインする（単にコンソール画面を切り替えるだけでは反映されないことが多い）。

- [ ] 動作確認

```bash
docker run hello-world
docker compose version
```

---

## 3. リポジトリのclone・環境ファイルの配置

### 3.1 GitHub Deploy Keyの発行・登録

Privateリポジトリ（`festallypicco/gmo-btc-ws`）にVPSからアクセスするため、**このVPS専用のSSH鍵を新規発行し、GitHubにDeploy Key（読み取り専用）として登録する。** 手元PCのGit認証情報をそのままVPSにコピーするのではなく、VPS用に新規発行するのがポイント（漏洩時の被害範囲を最小化するため）。

**1. VPS上（`takuya`ユーザー）で鍵ペアを新規生成する**

```bash
ssh-keygen -t ed25519 -C "vps-gmo-btc-ws" -f ~/.ssh/id_ed25519_deploy
```
（パスフレーズは空Enterでよい）

**2. 公開鍵を表示する**

```bash
cat ~/.ssh/id_ed25519_deploy.pub
```

`ssh-ed25519 AAAA...vps-gmo-btc-ws`という**1行全体**（末尾のコメント部分も含む）をコピーする。

**3. GitHubにDeploy Keyとして登録する**

ブラウザで`https://github.com/festallypicco/gmo-btc-ws` → `Settings` → `Deploy keys` → `Add deploy key`を開き、
- Title: 分かりやすい名前（例: `ConoHa VPS`）
- Key: 手順2でコピーした1行を貼り付け
- **「Allow write access」にはチェックを入れない**（読み取り専用にする）

**4. SSH configに設定を追加する**

```bash
cat >> ~/.ssh/config << 'EOF'
Host github.com
    IdentityFile ~/.ssh/id_ed25519_deploy
    IdentitiesOnly yes
EOF
```

### 3.2 clone

```bash
sudo mkdir -p /opt/gmo-btc-ws
sudo chown takuya:takuya /opt/gmo-btc-ws
cd ~
git clone git@github.com:festallypicco/gmo-btc-ws.git /opt/gmo-btc-ws
```

初回はGitHubのホスト鍵確認が出るので`yes`と答える。

**`Permission denied (publickey)`になる場合** → 3.1節のDeploy Key登録をやり直す。公開鍵をコピーする際に末尾のコメント部分を含め忘れていないか、改行や空白が混ざっていないかを確認する。

### 3.3 `.env`・`ai_review/.env`の配置

`.gitignore`対象のため、cloneしただけではVPSに存在しない。手元PCから個別に転送する。

**手元PCのPowerShellで（プロジェクトルートで実行）**

```powershell
cd $LOCAL_REPO
scp -P $SSH_PORT -i $PEM .env ${VPS_USER}@${VPS_IP}:/opt/gmo-btc-ws/.env
scp -P $SSH_PORT -i $PEM ai_review\.env ${VPS_USER}@${VPS_IP}:/opt/gmo-btc-ws/ai_review/.env
```

**VPS側で転送確認**

```bash
ls -la /opt/gmo-btc-ws/.env /opt/gmo-btc-ws/ai_review/.env
```

**チェックリスト（値が過不足なく揃っているか目視確認）**

```bash
cat /opt/gmo-btc-ws/.env | grep -E "GMO_API_KEY_TRADE|GMO_API_KEY_READONLY|X_API_KEY|TELEGRAM_SNS_BOT_TOKEN|LLM_PROVIDER"
cat /opt/gmo-btc-ws/ai_review/.env | grep -E "TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID"
```

- `.env`側: `GMO_API_KEY_TRADE`・`GMO_API_SECRET_TRADE`（発注用）、`GMO_API_KEY_READONLY`・`GMO_API_SECRET_READONLY`（参照専用）
- `.env`側: X投稿用`X_API_KEY`・`X_API_SECRET`・`X_ACCESS_TOKEN`・`X_ACCESS_TOKEN_SECRET`
- `.env`側: SNS投稿用`TELEGRAM_SNS_BOT_TOKEN`・`TELEGRAM_SNS_CHAT_ID`（内部管理用の`TELEGRAM_BOT_TOKEN`とは別物）
- `.env`側: `LLM_PROVIDER`・`LLM_API_KEY`
- `ai_review/.env`側: `TELEGRAM_BOT_TOKEN`・`TELEGRAM_CHAT_ID`

**⚠️ `GMO_API_KEY_READONLY`／`GMO_API_SECRET_READONLY`は、必ず「実資金用の口座を参照できるキー」になっているか確認すること。** 詳しくは7.0節を参照（仮想資金用のデモキーがそのまま入っていて、本番切替後にエラーになった実例あり）。

**パーミッションを絞る**

```bash
chmod 600 /opt/gmo-btc-ws/.env
chmod 600 /opt/gmo-btc-ws/ai_review/.env
```

### 3.4 `runtime/`の初期ファイル作成

```bash
ls -la /opt/gmo-btc-ws/runtime/
```

`.gitkeep`しか無いはずなので、bind mount時にDockerが空ディレクトリを自動生成してしまう事故を防ぐため、空ファイルとして先に作成しておく。

```bash
touch /opt/gmo-btc-ws/runtime/live_state.db
touch /opt/gmo-btc-ws/runtime/trading_engine.pid
```

### 3.5 `config/config.json`の配置

`.gitignore`対象のため、こちらも手元PCから転送する。**確定状態のタイミングを選んで**転送する。

- コピー元（手元PC）の`config.json`が、直近の03:30のAI夜間レビュー・06:00の設定反映後の確定状態であること
- コピー前に`pending_rollouts`が空になっていること（段階的ロールアウトの途中でないこと）

**手元PCのPowerShellで**

```powershell
cd $LOCAL_REPO
scp -P $SSH_PORT -i $PEM config\config.json ${VPS_USER}@${VPS_IP}:/opt/gmo-btc-ws/config/config.json
```

**VPS側で確認**

```bash
ls -la /opt/gmo-btc-ws/config/config.json
grep trading_mode /opt/gmo-btc-ws/config/config.json
```

**⚠️ `trading_mode`の値を必ず目視確認すること。** ディレクトリ構造をそのまま引き継ぐと、ネイティブ運用時代の設定（`"virtual"`）がそのままVPSに移設されてしまうことがある。実資金運用に切り替えるつもりであれば、この時点、または7章の本番切替時点で`"real"`になっていることを必ず確認する（切り替え方法は7.0節）。

### 3.6 監視系スクリプト用Python環境の構築（venv）

**この節は、`cd /opt/gmo-btc-ws`から始まる内容のため、3.2節のcloneが完了してから実施する。**（旧版の手順書では2章に置かれていて、実行順と矛盾していたため、この位置に移動している）

`check_trading_anomaly.py`・`check_csv_db_consistency.py`・`check_monitor_heartbeats.py`・`check_crash_loop.py`・`check_orphan_orders.py`は、Dockerコンテナの中身ではなくVPSホスト側のファイルシステムを直接読む設計のため、VPSホスト自体にPython3実行環境が必要。

```bash
sudo apt install -y python3 python3-venv python3-pip
cd /opt/gmo-btc-ws
python3 -m venv .venv-host
source .venv-host/bin/activate
pip install -r btc_trading_tool/requirements.txt -r ai_review/requirements.txt
pip install -r scripts/requirements.txt
deactivate
```

**確認**

```bash
source /opt/gmo-btc-ws/.venv-host/bin/activate
pip list
deactivate
```

以下が入っていることを確認する（パッケージ名の表記ゆれに注意。例えば「Gemini用SDK」は`google-generativeai`ではなく`google-genai`という名前で入っていることがある）。
- `tweepy`（X投稿）
- `google-genai`または`openai`（`.env`の`LLM_PROVIDER`の設定に応じてどちらか一方）
- `Pillow`・`imageio-ffmpeg`（SNS動画生成）

```bash
which ffmpeg
```
何も表示されなければ`sudo apt install -y ffmpeg`（`imageio-ffmpeg`同梱バイナリで足りることが多いが念のため）。

**⚠️ 実際に本番切替後に発覚した重要な落とし穴（8.10節も参照）**: `scripts/run_crash_loop_check.sh`・`scripts/run_orphan_orders_check.sh`の2本は、他の監視スクリプト（`run_anomaly_check.sh`等）とは別に修正されたもので、**`python`コマンドを裸で（フルパス指定なしで）呼んでいる箇所が残っていた。** VPSには`python`という名前のコマンドは存在せず（`python3`のみ）、`python: command not found`でエラーになる。以下で該当箇所を確認し、venv内Pythonの絶対パスに書き換えておくこと。

```bash
grep -n "^python \|[^_]python \"" /opt/gmo-btc-ws/scripts/run_crash_loop_check.sh /opt/gmo-btc-ws/scripts/run_orphan_orders_check.sh
```

もし裸の`python`呼び出しが見つかったら、`"$PROJECT_ROOT/.venv-host/bin/python"`（両ファイルとも`PROJECT_ROOT`という変数がすでに定義されている）に置き換える。詳しい修正例は8.10節を参照。**この確認は、他のスクリプトを追加・変更した際にも毎回行う習慣をつけること**（同じ問題が今後別のスクリプトでも起こりうるため）。

---

## 4. Dockerイメージのビルド・起動確認（本番切り替え前のテスト）

### 4.1 テスト用構成（`docker-compose.test.yml`）での基本動作確認

**⚠️ `docker-compose.test.yml`は、本番用`docker-compose.yml`とは別の、隔離されたテストデータ（`docker_test/config`・`docker_test/log`・`docker_test/runtime`）を使う設計になっている。** 3.4〜3.6節で用意した本番用の`config/`・`runtime/`とは別物なので、混同しないこと。また`.env`も読み込まない設計（実際のGMOコイン認証情報を使わない、仮想モード限定のテスト）。

```bash
cd /opt/gmo-btc-ws
docker compose -f docker-compose.test.yml build --no-cache
docker compose -f docker-compose.test.yml up -d
docker compose -f docker-compose.test.yml ps
```

`engine`・`dashboard`両方が`Up`になっていることを確認する。

**ログ確認**

```bash
docker compose -f docker-compose.test.yml logs engine
```

**`live_state.db`が更新され続けているか確認（`docker_test/`側のパスであることに注意）**

```bash
stat -c '%y' /opt/gmo-btc-ws/docker_test/runtime/live_state.db
```
30秒ほど待って再実行し、時刻が進んでいることを確認する。

**`manual_stop.flag`による安全停止の確認**

このディレクトリはDockerが自動生成した際に`root`所有になっていることが多いため、`sudo`を付ける。

```bash
sudo touch /opt/gmo-btc-ws/docker_test/runtime/manual_stop.flag
docker compose -f docker-compose.test.yml logs engine | tail -30
```

`[Engine] manual stop requested and position is flat. shutting down safely.`のようなログが出て、正常にシャットダウンすることを確認する。確認できたらフラグを削除して再起動を確認する。

```bash
rm /opt/gmo-btc-ws/docker_test/runtime/manual_stop.flag
docker compose -f docker-compose.test.yml logs -f engine
```
（`Ctrl+C`で監視終了）

確認が終わったら停止する。

```bash
docker compose -f docker-compose.test.yml down
```

### 4.2 本番用`docker-compose.yml`でのログ永続化・`PYTHONUNBUFFERED`の検証

本番パス（`./config`・`./log`・`./runtime`）を直接使うと3.4〜3.6節のデータに影響するため、**検証専用パスに一時的に差し替えて確認する。**

**1. 検証専用ディレクトリを用意**

```bash
mkdir -p /opt/gmo-btc-ws/config-verify /opt/gmo-btc-ws/log-verify /opt/gmo-btc-ws/runtime-verify
cp /opt/gmo-btc-ws/docker_test/config/config.json /opt/gmo-btc-ws/config-verify/config.json
touch /opt/gmo-btc-ws/runtime-verify/live_state.db
touch /opt/gmo-btc-ws/runtime-verify/trading_engine.pid
```

**2. `docker-compose.override.yml`で一時的にパスを上書き**

```bash
tee /opt/gmo-btc-ws/docker-compose.override.yml > /dev/null << 'EOF'
services:
  engine:
    volumes:
      - ./config-verify:/app/config
      - ./log-verify:/app/log
      - ./runtime-verify:/app/runtime
  dashboard:
    volumes:
      - ./config-verify:/app/config
      - ./log-verify:/app/log
      - ./runtime-verify:/app/runtime
EOF
```

**3. ビルド・起動**

```bash
docker compose build --no-cache
docker compose up -d
```

**4. ログファイル生成確認**

```bash
ls -la /opt/gmo-btc-ws/log-verify/
```

`engine_YYYY-MM-DD.log`・`.err.log`等が生成されているか確認する。

**5. リアルタイム追記の確認（`PYTHONUNBUFFERED`が効いているか）**

```bash
tail -f /opt/gmo-btc-ws/log-verify/engine_YYYY-MM-DD.log
```
数秒おきに新しい行が追記されれば正常（バッファリング問題があれば、しばらく何も表示されない）。`Ctrl+C`で終了。

**6. `docker compose down`後もログが残ることを確認**

```bash
docker compose down
ls -la /opt/gmo-btc-ws/log-verify/
```

**7. 後片付け**

```bash
rm -rf /opt/gmo-btc-ws/config-verify /opt/gmo-btc-ws/log-verify /opt/gmo-btc-ws/runtime-verify
rm /opt/gmo-btc-ws/docker-compose.override.yml
```
（`docker-compose.override.yml`の削除時、write protectedの確認が出た場合は`y`で進めてよい）

---

## 5. systemdファイルの配備

### 5.1 配備

```bash
cd /opt/gmo-btc-ws
sudo cp deploy/systemd/ai-review.* /etc/systemd/system/
sudo cp deploy/systemd/engine-restart.* /etc/systemd/system/
sudo cp deploy/systemd/crash-loop-check.* /etc/systemd/system/
sudo cp deploy/systemd/anomaly-check.* /etc/systemd/system/
sudo cp deploy/systemd/csv-consistency-check.* /etc/systemd/system/
sudo cp deploy/systemd/monitor-heartbeat-check.* /etc/systemd/system/
sudo cp deploy/systemd/orphan-orders-check.* /etc/systemd/system/
sudo cp deploy/systemd/public-report.* /etc/systemd/system/
sudo cp deploy/systemd/sns-report.* /etc/systemd/system/
sudo systemctl daemon-reload
```

### 5.2 `WorkingDirectory`の確認

```bash
grep WorkingDirectory /etc/systemd/system/ai-review.service \
  /etc/systemd/system/engine-restart.service \
  /etc/systemd/system/crash-loop-check.service \
  /etc/systemd/system/anomaly-check.service \
  /etc/systemd/system/csv-consistency-check.service \
  /etc/systemd/system/monitor-heartbeat-check.service \
  /etc/systemd/system/orphan-orders-check.service \
  /etc/systemd/system/public-report.service \
  /etc/systemd/system/sns-report.service
```

すべて`WorkingDirectory=/opt/gmo-btc-ws`になっていることを確認する。

### 5.3 構文確認

```bash
for svc in ai-review engine-restart crash-loop-check anomaly-check csv-consistency-check monitor-heartbeat-check orphan-orders-check public-report sns-report; do
  sudo systemd-analyze verify /etc/systemd/system/${svc}.service
done
```

エラーが出なければOK。あわせて以下も確認する。

```bash
file /etc/systemd/system/ai-review.* /etc/systemd/system/engine-restart.*
```
→ `CRLF`という表示が出ていないこと

```bash
grep TimeoutStartSec /etc/systemd/system/ai-review.service
grep ExecStart /etc/systemd/system/ai-review.service
```
→ `TimeoutStartSec=0`、`ExecStart`がコンテナ内絶対パス（`/app/ai_review/run_nightly_review.sh`）になっていること

**この時点ではまだ`systemctl enable --now`は実行しない**（7章の本番切り替えと同じタイミングで有効化する）。

---

## 6. SSHトンネル経由でのダッシュボードアクセス確認

**手元PC側から実行**（トンネルを繋いだセッションは、確認が終わるまで閉じずにおく）

```powershell
ssh -p $SSH_PORT -i $PEM -L 8501:localhost:8501 ${VPS_USER}@${VPS_IP}
```

トンネルを繋いだまま、そのままWindowsのブラウザで以下にアクセスする。

```
http://localhost:8501
```

**補足**: コマンド内の`localhost`はVPS側から見たlocalhostを指し、ブラウザで入力する`localhost:8501`は手元PC側から見たlocalhostを指す。SSHトンネルがこの間の通信を裏側で転送してくれるので、`$VPS_IP`をブラウザに直接入力する必要はない。

ダッシュボードを表示するには、VPS側でいずれかのdashboardコンテナが起動している必要がある（4章のテスト起動、または7章の本番起動のどちらでもよい）。表示されない場合は、VPS側で`docker ps`を確認し、コンテナが起動しているか確認する。

- [ ] `docker-compose.yml`側で`dashboard`サービスが`127.0.0.1:8501`のみにバインドされている（外部非公開）ことを確認する

---

## 7. ネイティブ→Docker本番切り替え（土曜09:00〜11:00のメンテナンス枠で実施）

**ここまでの手順（1〜6章）が全て完了し、動作確認が取れてから実施すること。**

### 7.0 ⚠️ 実資金運用にするなら、切り替え前に必ず確認すること

**実際にここでの確認不足により、本番切替後にトラブルが2件発生した。** 事前に必ず確認しておく。

**1. `config.json`の`trading_mode`が`"real"`になっているか**

```bash
grep trading_mode /opt/gmo-btc-ws/config/config.json
```

ネイティブ運用時代のディレクトリ構造をそのまま引き継ぐと、`"virtual"`のまま移設されてしまうことがある。実資金運用のつもりであれば、`"real"`に書き換える。

```bash
cp /opt/gmo-btc-ws/config/config.json /opt/gmo-btc-ws/config/config.json.bak_$(date +%Y%m%d)
sed -i 's/"trading_mode": "virtual"/"trading_mode": "real"/' /opt/gmo-btc-ws/config/config.json
grep trading_mode /opt/gmo-btc-ws/config/config.json
```

**2. `.env`の`GMO_API_KEY_READONLY`／`GMO_API_SECRET_READONLY`が「実資金用口座の」参照専用キーになっているか**

`GMO_API_KEY_TRADE`（発注用）と混同しやすいが、これは別のキー。`check_orphan_orders.py`（孤児注文監視、5分毎に実行）が使う。**「読み取り専用」という権限の強さと、「どの口座に対応するキーか」は別の軸であることに注意。** 「実資金用の、読み取り専用キー」を用意する必要がある。

GMOコインの管理画面のAPIキー一覧と、`.env`の値を照らし合わせて確認する。

```bash
grep GMO_API_KEY_READONLY /opt/gmo-btc-ws/.env
grep GMO_API_SECRET_READONLY /opt/gmo-btc-ws/.env
```

**もし「実資金用の参照専用キー」がまだGMOコイン側に存在しない場合、新規発行する。** 権限設定は以下の通り。

| 項目 | 設定 |
|---|---|
| ラベル | 分かりやすい名前（例: `BTC自動売買本番用-参照専用`） |
| IP制限 | VPSのIPアドレス |
| 資産＞資産残高を取得 | ✓ |
| 資産＞余力情報を取得 | ✓ |
| トレード＞有効注文一覧 | ✓（`check_orphan_orders.py`が使う中心的な権限） |
| トレード＞建玉一覧を取得 | ✓ |
| トレード＞建玉サマリーを取得 | 任意（あれば安心） |
| トレード＞注文情報取得・約定情報取得・最新の約定一覧 | 任意 |
| **トレード＞注文・決済注文・注文変更・ロスカットレート変更・注文キャンセル系・一括決済注文** | **✗ 絶対にチェックしない** |
| 資産＞入出金・振替系 | ✗ 不要 |
| WebSocket系 | ✗ 不要（`check_orphan_orders.py`はWebSocketを使わない） |

発行できたら、`.env`の`GMO_API_KEY_READONLY`／`GMO_API_SECRET_READONLY`をその値に書き換える。

**この2点を確認・修正してから、以下の切り替え手順に進む。**

### 7.1 事前準備（メンテナンス枠の少し前）

```bash
cd /opt/gmo-btc-ws
docker compose -f docker-compose.yml build --no-cache
```

### 7.2 切り替え手順（メンテナンス時間帯内）

1. [ ] **ローカルPC側**: タスクスケジューラで以下をすべて無効化する。**特にX投稿・SNS配信の無効化を忘れると、ローカルPCとVPSの両方から同じ内容が二重に投稿・配信される事故になる。**

```powershell
Disable-ScheduledTask -TaskName "BTC_Engine_Restart"
Disable-ScheduledTask -TaskName "BTC_Detect_Orphan_Engines"
Disable-ScheduledTask -TaskName "BTC_Public_Report_Post"
Disable-ScheduledTask -TaskName "BTC_SNS_Report_Post"
Disable-ScheduledTask -TaskName "BTC_Engine_Process_Check"
```

（この時点ではタスクを**削除せず「無効化」のままにしておく**。VPS側が数日〜1週間、問題なく安定稼働することを確認できるまでは、ロールバック手段として残しておくと安全）

2. [ ] **ローカルPC側**: ネイティブのエンジン・ダッシュボードプロセスが本当に止まっているか確認する

```powershell
Get-Process | Where-Object { $_.ProcessName -like "*python*" }
```
何も表示されないことを確認する。

3. [ ] **ローカルPC側**: `docker-compose.real-test.yml`環境が起動していないか確認する（本番とreal-testは発注用APIキーを共有しており、二重発注のリスクがあるため）。**必ずプロジェクトのルートディレクトリで実行すること**（`C:\WINDOWS\system32`など無関係な場所で実行すると単なるファイル不在エラーになり紛らわしい）

```powershell
cd $LOCAL_REPO
docker compose -f docker-compose.real-test.yml ps
```
`NAME`欄に何も表示されていない（Upのコンテナが無い）ことを確認する。動いていれば`down`してから次へ進む。

4. [ ] **VPS側**: 本番コンテナを起動する前に、`config/`・`log/`・`runtime/`ディレクトリの所有者を確認しておく（4章のテストでDockerが自動生成したディレクトリが`root`所有のままだと、監視スクリプトが権限エラーになる）

```bash
ls -la /opt/gmo-btc-ws/log/ /opt/gmo-btc-ws/runtime/
# rootが混ざっていれば
sudo chown -R takuya:takuya /opt/gmo-btc-ws/log /opt/gmo-btc-ws/runtime
```

5. [ ] **VPS側**: 本番コンテナを起動する

```bash
docker compose -f docker-compose.yml up -d
```

6. [ ] **VPS側**: 起動確認

```bash
docker ps
```
`btc-engine`・`btc-dashboard`が`Up`であることを確認する。

7. [ ] **VPS側**: エンジンのログで、`trading_mode`が意図通り（`real`）になっているか確認する

```bash
docker logs btc-engine --tail 20
```
`trading_mode=real`と出ていること、実資金の場合は`[PrivateWebSocketManager]`の認証・接続ログが出ていることを確認する。

8. [ ] **ローカルPC側**: プロセス一覧で、ネイティブのエンジン・ダッシュボードが起動していないことを再確認する（`docker ps`とのダブルチェック）

```powershell
Get-Process | Where-Object { $_.ProcessName -like "*python*" }
```

9. [ ] **VPS側**: systemdタイマーを有効化する

```bash
for t in ai-review engine-restart crash-loop-check anomaly-check csv-consistency-check monitor-heartbeat-check orphan-orders-check public-report sns-report; do
  sudo systemctl enable --now ${t}.timer
done
sudo systemctl list-timers | grep -E "ai-review|engine-restart|crash-loop-check|anomaly-check|csv-consistency-check|monitor-heartbeat-check|orphan-orders-check|public-report|sns-report"
```

9つ全てに`NEXT`（次回実行予定時刻）が表示されていることを確認する。

10. [ ] **直近のタイマー実行結果を確認する**（初回実行が数十分以内にある場合は、待って結果を見ておく）

```bash
sudo journalctl -u crash-loop-check.service -n 20 --no-pager
sudo journalctl -u orphan-orders-check.service -n 20 --no-pager
```
`python: command not found`のようなエラーが出た場合は3.6節の対応（venv明示パス化）が漏れている箇所がないか再確認する。GMO APIの`ERR-5012`（Invalid API-KEY, IP, or permissions）が出た場合は7.0節を再確認する。

### 7.3 切り替え後の確認（当日〜翌朝にかけて）

- [ ] 6章の手順でSSHトンネル経由のダッシュボードアクセスを再確認する
- [ ] 翌日03:30のAI夜間レビュー（`journalctl -u ai-review.service`）
- [ ] 翌日06:00のengine再起動（`journalctl -u engine-restart.service`）
- [ ] Telegramに日次レポート（05:55）・AI議論結果（03:30以降）が通常通り届くこと
- [ ] 翌日06:10のX投稿（`journalctl -u public-report.service`）と、Xのプロフィールページに反映されていること（プロフィール未整備等の理由で投稿を1回だけ止めたい場合は8.11節を参照）
- [ ] 翌日06:15のSNS配信（`journalctl -u sns-report.service`）と、SNS専用Botに動画＋テキストが届いていること
- [ ] ローカルPC側から重複投稿・重複配信が発生していないこと
- [ ] `runtime/monitor_heartbeats.json`のハートビートが途切れていないこと

---

## 8. トラブルシューティング集（実際に発生した事象と対応）

### 8.1 SSH接続ができない（全体の切り分け）

エラーメッセージの種類で、どの層の問題かある程度絞り込める。

| エラー | 疑うべき層 |
|---|---|
| `Connection timed out` | ConoHaのセキュリティグループ（1.2節） |
| `Connection refused` | サーバー内部のsshd設定（1.8節、ポート未反映等） |
| `Permission denied (publickey)` | クライアント側の鍵指定、またはサーバー側の`authorized_keys`（1.5節） |

### 8.2 ConoHaのセキュリティグループを作成したのに繋がらない

「セキュリティグループの管理」画面でルールを作成しただけで、対象サーバーの「ネットワーク情報」タブで実際に割り当てていないケース。1.2節の手順で、サーバー個別の割り当て状況を確認する。

### 8.3 `authorized_keys`が空、または存在しない

`root`の`authorized_keys`自体が空（0バイト）になっていることがある（ConoHa側の鍵管理の都合と思われる）。この場合、`root`からコピーしようとしても空のままなので、1.5節の「方法A」（手元PCの秘密鍵から直接公開鍵を導出して貼り付け）を使う。

### 8.4 PowerShellの`cat`パイプでの公開鍵登録失敗

`cat ~/.ssh/id_ed25519.pub | ssh ... "... >> authorized_keys"`のようなワンライナーが失敗する場合、PowerShellの`cat`（`Get-Content`のエイリアス）がBOM付与・改行コード変換を行い、SSH側で鍵として認識されないことがある。VPS側で直接`authorized_keys`を作成し、`ssh-keygen -y -f <秘密鍵パス>`で表示した公開鍵をコピー＆ペーストで貼り付ける方式に切り替える（1.5節参照）。

### 8.5 サービス名`sshd`が見つからない

```
sudo systemctl restart sshd
Unit sshd.service not found.
```

Ubuntu 24.04ではユニット名が`ssh`（`sshd`ではない）。`sudo systemctl restart ssh`を使う。

### 8.6 `PasswordAuthentication no`にしたのに実効値が`yes`のまま

`sshd_config`本体は正しく`no`になっていても、`/etc/ssh/sshd_config.d/50-cloud-init.conf`（cloud-init自動生成）内の設定が、`Include`の読み込み順序の都合で優先されてしまうことがある。`sudo sshd -T | grep passwordauthentication`で実効値を必ず確認する（1.7節参照）。

### 8.7 ポート変更したのに繋がらない（ソケットアクティベーション）

Ubuntu 22.04以降は、`sshd_config`の`Port`変更だけでは反映されない。`systemctl status ssh`の出力に`TriggeredBy: ● ssh.socket`とあれば該当。`/etc/systemd/system/ssh.socket.d/listen.conf`にIPv4/IPv6両方明示で上書き設定を追加する（1.8節参照）。

### 8.8 GitHubへのclone時に`Permission denied (publickey)`

VPS用のDeploy Keyがまだ発行・登録されていない。3.1節の手順を実施する。公開鍵をコピーする際は、末尾のコメント部分（`ssh-keygen`の`-C`オプションで指定した文字列）も含めて1行全体をコピーすること。

### 8.9 ブラウザコンソールで`nano`のショートカットが効かない

`Ctrl+O`（保存）・`Ctrl+X`（終了）・`Ctrl+W`（検索）などが、ブラウザ自体のショートカットと衝突して効かないことがある。

- **対策1（推奨）**: `tee`コマンドでエディタを使わずファイルを直接作成する
  ```bash
  sudo tee /path/to/file << 'EOF'
  （書きたい内容）
  EOF
  ```
- **対策2**: どうしても`nano`を使いたい場合、ブラウザのウィンドウを最大化する（ウィンドウが小さいと、入力中のカーソル位置が画面外にスクロールして見えなくなり、反応していないように見えることがある）。コンソール画面内を一度クリックしてフォーカスを当ててから操作する

### 8.10 監視系スクリプトが`python: command not found`で失敗する

VPSには`python`という名前のコマンドは存在しない（`python3`のみ）。`scripts/run_*.sh`系のスクリプトが、venv内Pythonの絶対パスではなく裸の`python`を呼んでいる箇所が残っていると発生する。

```bash
grep -n "python" /opt/gmo-btc-ws/scripts/<対象スクリプト>.sh
```
で該当箇所を確認し、`"$PROJECT_ROOT/.venv-host/bin/python"`（`PROJECT_ROOT`変数がスクリプト内にすでに定義されていることが多い）に置き換える。修正後は必ず以下を確認する。

```bash
bash -n /opt/gmo-btc-ws/scripts/<対象スクリプト>.sh   # 構文チェック
sudo -u takuya bash /opt/gmo-btc-ws/scripts/<対象スクリプト>.sh   # 手動実行での動作確認
```

**この修正はVPS側のファイルだけでなく、Cursor側のリポジトリ本体にも反映しておくこと。** そうしないと次回`git pull`した際に同じ問題が再発する。

### 8.11 `manual_stop.flag`等の作成時に`Permission denied`

`runtime/`・`log/`・`config/`ディレクトリが、Docker起動時に自動生成された際の所有者（`root`）のままになっていることがある（コンテナ内プロセスが`root`で動いているため）。

```bash
sudo chown -R takuya:takuya /opt/gmo-btc-ws/<対象ディレクトリ>
```
で解消する（コンテナ動作には影響しない。ホスト側のスクリプトから直接触る際の権限の話）。

### 8.12 `docker`コマンドが`permission denied`（Docker APIソケット）

```
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

`docker`グループへの追加が、現在のセッションにまだ反映されていない。`groups`で確認し、含まれていなければ一度完全にログアウトしてから再接続する（2章参照）。

### 8.13 `docker-compose.test.yml`のログ・状態ファイルが見つからない

`docker-compose.test.yml`は`docker_test/config`・`docker_test/log`・`docker_test/runtime`という、本番用の`config/`・`log/`・`runtime/`とは**別の隔離パス**を使う設計になっている。`live_state.db`の更新確認等をする際は、どちらのcomposeファイルで起動したコンテナかによって確認すべきパスが変わることに注意する（4.1節参照）。

### 8.14 GMO APIキーが`ERR-5012`（Invalid API-KEY, IP, or permissions）で失敗する

`GMO_API_KEY_TRADE`（発注用）は正常に動いているのに、`GMO_API_KEY_READONLY`（参照専用、`check_orphan_orders.py`が使用）だけがこのエラーになる場合、`.env`の`READONLY`欄に仮想資金（デモ）用のキーがそのまま入っている可能性が高い。GMOコインの管理画面のAPIキー一覧と照合し、実資金用の参照専用キーに差し替える（7.0節参照）。

**この問題はネイティブPC運用時には表面化しない。** `check_orphan_orders.py`は`trading_mode≠real`の場合、GMOへのAPI呼び出し自体をスキップする設計のため、`virtual`のまま運用している間はキーが正しいかどうか検証されない。本番切替（`trading_mode=real`）で初めて、このコードパスが実際に実行され、隠れていた設定ミスが表面化する。**そのため、本番切替の前に7.0節のチェックを必ず行うこと。**

### 8.15 X投稿を1回だけ休みたい

```bash
sudo systemctl stop public-report.timer
sudo systemctl disable public-report.timer
```

再開時（休みたい期間が終わったら）

```bash
sudo systemctl enable --now public-report.timer
sudo systemctl list-timers | grep public-report
```

---

## 9. 参照先・変更履歴

### 9.1 困ったときの調べ方

- 各手順の設計判断の経緯は仕様書（`BTC自動売買システム_仕様書.md`）の4.5節を参照
- 日常運用（緊急停止・再開・Telegram通知の見方等）は`運用マニュアル.md`を参照
- Cursorに追加の実装を依頼する場合は、`運用マニュアル.md`7章の注意点（項番を含めない、絵文字禁止、依頼内容以外を追加しない）に従うこと

### 9.2 この手順書に含まれていない・別途対応が必要な事項

- **VPS安定稼働確認後のネイティブ側タスクの整理**: 7.2節でタスクスケジューラを「無効化」のみに留めているのは、ロールバック手段を残すため。VPSが数日〜1週間問題なく稼働することを確認できてから、削除するかどうか判断する
- **PARAMETER_ABSOLUTE_BOUNDS等のパラメータ調整**: VPS移行そのものとは独立した課題
- **内部日次レポート（`BTC_Daily_Report`、05:55）に対応するsystemdタイマー**: 5章のリストに含まれているか、Windows側の全タスクと突き合わせて漏れがないか要確認
- **両compose（本番／real-test）同時起動時の二重発注防止**: `runtime/gmo_trade_key.lock`による排他ロックは、本番とreal-testでruntimeパスが別のため、このロックだけでは両compose間の同時起動を防げない。7.2節の手順3（real-test未起動確認）でカバーしているが、コードレベルでの防止策ではない点に注意

### 9.3 次回、別のVPS・別の環境を構築する際のチェックリスト

- [ ] SSH鍵を作成・登録した
- [ ] ConoHa等のクラウド側で、目的のポートを含むセキュリティグループが（`default`のままではなく）サーバーに割り当てられている
- [ ] 作業用ユーザーへの公開鍵登録は、`root`の`authorized_keys`をコピーするのではなく、秘密鍵から直接公開鍵を導出して貼り付ける方式を優先する
- [ ] ポートを変更する場合、`systemctl status ssh`で`ssh.socket`経由か確認し、該当すれば`ssh.socket.d/listen.conf`をIPv4/IPv6両方明示で作成した
- [ ] `PasswordAuthentication`等の設定変更後は`sshd -T`で実効値を確認する習慣をつける
- [ ] ブラウザコンソールでファイル編集する際は`tee`方式を優先する
- [ ] Docker/dockerグループ追加後は、必ず一度完全にログアウト・再ログインしてから確認する
- [ ] 本番切替前に、APIキー等の認証情報が「本番用」か「テスト用」か、値まで目視で照合する（変数名やラベルだけで判断しない）
- [ ] host側で動く監視スクリプトは、追加・変更のたびに`python`コマンドの裸呼び出しが残っていないか確認する

### 9.4 変更履歴（要約）

- **v11（2026-08-02）**: 2026-08-01のVPS実機構築（ConoHa）で発生した一連のつまずきを全て反映し、全面書き直し。ConoHaのセキュリティグループ、SSH鍵認証、ソケットアクティベーション、Docker権限、docker-compose.test.ymlとdocker_test/パスの区別、venv内Python明示パス化の水平展開、GMO APIキーのREADONLY取り違え等を追加
- **v10（2026-08-01）**: 旧2.1節（監視系スクリプト用Python環境構築）を3.5節へ移動（実行順と章番号の矛盾を解消）
- **v5〜v9（2026-07-17〜31）**: config.json転送手順、systemd配備（7セット→9セット）、PYTHONUNBUFFERED対応、SNS投稿機能追加への対応等を順次反映
