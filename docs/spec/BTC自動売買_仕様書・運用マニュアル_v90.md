# BTC/JPY 自動売買システム 仕様書・運用マニュアル

最終更新: 2026-08-21（v90）

**v90での変更点**: 2026-08-19〜21に発生した一連のインシデント（重複ログ・15時間停止・order_rate_limit頻発）の根本原因調査と対応を行った（詳細は7.27）。(1) `build_daily_report.py`の`append_daily_history()`に、直前行と同一`trading_day`への重複追記を防ぐガードを追加した（3.4.52）。(2) `trading_day_date`のロールオーバーがエンジン再起動時にしか行われず、`engine-restart`がSKIPされると日次リセットが実時刻からずれたまま継続する問題（3.4.8の既知の制約だった箇所）に対応し、通常メインループ・PAUSEDループの両方に実時刻ベースの日付比較によるロールオーバー処理を追加した（3.4.53）。既存の06:00固定`engine-restart`とは冪等に共存する設計とした。(3) 2026-08-20の相場急変時、imbalanceキャンセル後の同一方向再エントリー（5〜10秒帯）と反対側への即時切替が組み合わさり`order_rate_limit`（1分5回）が頻発、これが(2)の引き金になった可能性が高いと判断し、`ENTRY_COOLDOWN_AFTER_CANCEL_SEC`を5秒から10秒へ延長した（3.4.35関連、3.4.54）。実データによるシミュレーションで、対象期間の発火31件中30件が回避可能と確認した上での変更。いずれもVPS実機へ反映済み・SHA-256一致確認済み。

**v89での変更点**: 2026-08-08 11:00ちょうどに`check_orphan_orders`から誤アラートが1通届いた事象（v88でのメンテナンス時間帯スキップ対応の境界漏れ）を受け、週次メンテナンス判定の終了側猶予を見直した。GMOの取引所仕様（メンテナンス終了後11:00〜11:10は「プレ・オープン」として注文取消のみ受付、11:10から通常注文可能）に合わせ、`is_weekly_maintenance_window()`の実効終了時刻を11:00から11:10へ変更（実効区間: 土曜`[08:55, 11:10)`）。この変更は`is_gmo_scheduled_maintenance_window()`を参照する全箇所（孤児注文監視・エンジンの`_is_entry_blocked()`）に共通で適用され、エンジン側の新規エントリー再開も11:10にずれる（強制決済は週次プレメンテ枠でのみ発火する設計のため影響なし）。あわせて、今回の調査過程で発見された`test_maintenance_entry_block.py`（日次メンテ枠のエントリーブロック境界を検証する調査用テスト）のdocstring・クラス名が「日次のエントリーブロックは未実装」という古い誤った前提のまま残っていた点を修正した（実際には日次エントリーブロックは実装済みで機能しており、8/7の05:01エントリー持ち越しインシデントとは別問題であることを確認済み。3.2・7.26の「日次は新規停止のみ実装・強制決済は未実装」という記載自体は正しいまま変更なし）。テストは`test_check_orphan_orders.py`＋`test_maintenance_entry_block.py`で38件パス。VPS反映済み：孤児注文監視側（host側venv）は`git pull`、エンジン側はSHORT建玉を保持したまま`docker compose build`→`up -d --no-deps --force-recreate`でrecreate、integrity OK（jpy_gap=2.15、許容範囲内）、Public/Private WS再接続済み、`virtual_trader.py`のSHA-256一致を確認。実機で週次枠の境界（11:09=メンテ扱い、11:10=枠外）も確認済み。

**v88での変更点**: 2026-08-08（土）09:00〜11:00の週次メンテナンス時間帯に`check_orphan_orders`から5分間隔で`[ALERT] wrapper exception / error=check_orphan_orders.py exit=1`が繰り返し届いた事象を受け、対応した。原因は、`check_orphan_orders.py`が`trading_mode≠real`時のみAPI呼び出しをスキップする設計になっており、週次メンテナンス時間帯（プレメンテ枠含む）をスキップする分岐が存在しなかったこと。メンテナンス中はGMO APIが応答しない状態になるため、`fetch_active_orders()`で例外が発生し続けていたと推定される。実際の建玉・注文には影響なし（エンジン側の`_is_weekly_maintenance_window()`によるメンテナンス対応は正しく機能していた）。対応として、週次メンテナンス・プレメンテ枠・日次メンテナンスの各判定ロジックを`virtual_trader.py`にモジュール関数（`is_weekly_maintenance_window`/`is_daily_maintenance_window`/`is_weekly_pre_maintenance_window`/これらをまとめた`is_gmo_scheduled_maintenance_window`）として切り出し、`VirtualTrader`の既存メソッドと`check_orphan_orders.py`の両方がこれを参照する形にした（判定ロジックの二重実装によるズレを防止）。`check_orphan_orders.py`は`trading_mode≠real`のチェック直後にメンテナンス時間帯のスキップ判定を追加し、スキップ時も心拍は更新する（API・Telegramは呼ばない）。他の監視スクリプト（クラッシュループ検知・エンジンプロセス監視・CSV/DB整合性チェック・異常検知・心拍監視）はいずれもGMO APIを直接呼んでいないことを確認し、対応不要と判断した。テスト`tests/test_check_orphan_orders.py`が21件全てパス（メンテ枠スキップ・枠外実行・`maintenance_prepare_minutes`反映・エンジン判定との一致を追加）。VPS実機への反映はまだ行っておらず、次回8/15（土）のメンテナンス時間帯で誤アラートが発生しないことの確認待ち（3.4.51、9章）。

**v87での変更点**: `test_gmo_err5008_diag.py`が`.gitignore`の`/test_*`ルールに引っかかり強制追加された件を調査した結果、7.25のバグの再発ではなく、新規テストファイル自体が`tests/`配下ではなくリポジトリルート直下に作られたことが原因と判明した。今後の再発防止として、Cursorへの定型文に「新規テストファイルは`tests/`配下に作成すること」を追加した（9章）。

**v85での変更点**: ERR-5008（GMOタイムスタンプずれエラー）の根本原因調査のため、`_gmo_private_request()`に診断ログ（署名〜送信遅延`sign_to_send_ms`、往復時間`rtt_ms`、レスポンスの`Date`ヘッダー等）を追加した。ERR-5008発生時のみ出力し、通常時のログ量・エラーハンドリング挙動には変更なし（3.4.50）。VPS実機へ反映済みで、次回発生を待って原因の切り分けを行う。あわせて、3.4.48本文に残っていた「アラート未実装」という古い記述を、既に完了している3.4.49（連続失敗アラート）への参照に修正した。

**v84での変更点**: Instagramリールの自動投稿で、プロフィール一覧に表示されるサムネイルが、v77で追加した冒頭フックのフェードイン演出の途中（文字がほぼ見えない真っ黒に近いフレーム）になってしまっていた事象を確認・対応した。Instagram Reelsのコンテナ作成時に`thumb_offset`パラメータ（サムネイルフレームの位置、ミリ秒）を追加し、値はフェードイン関連の定数（`TEXT_FADE_SEC`・`HOOK_LINE_STAGGER_SEC`）から自動算出する形にした（4.7.8）。あわせて、9章に残っていた4.7.5の動作確認・3.4.48のアラート実装を完了として反映した。

**v83での変更点**: 2件対応した。(1) 2026-08-07、日次メンテナンス時間帯（05:55〜06:30）に持ち越されたポジションが強制決済されない事象が発生し調査した結果、日次窓には`_force_close_real()`への接続自体が実装当初から存在しないという設計と仕様書記載の乖離が判明した（週次プレメンテ枠・manual_stop.flag経由の強制決済は従来通り機能）。GMOレバレッジ取引の手数料構造（取引手数料実質無料、持ち越し時のみ0.04%/日のレバレッジ手数料）を踏まえて費用対効果を検討し、強制決済を新規実装するのではなく、現状の実装（新規エントリー停止のみ、既存ポジションは持ち越し）を維持し仕様書側を実態に合わせる方針とした（3.2節、7.26）。(2) X・Instagram/Threadsへの投稿時刻を、GMOメンテナンス時間帯と重なりSNS閲覧者も少ない06:10/06:15から、閲覧者が増える朝07:30/07:35へ変更し、VPS実機で反映・確認済み（4.5.11）。

**v82での変更点**: v76で設計・実装した板TP（REAL-TP）決済経路のsettle修正（3.4.47）について、VPS実機への反映状況を確認した。反映手順を明示的に踏んだ記録が無かったため未反映の可能性を懸念していたが、Cursorに調査（コード変更・デプロイは行わない調査限定の指示）させたところ、該当コミット（`c618bcb`、2026-08-03）はv77のSNSリール動画対応コミットに同梱される形で既にVPSにpull・反映済みであることが判明した。VPSリポジトリのコミット履歴、稼働中`btc-engine`コンテナ内`virtual_trader.py`の実体、両者のSHA-256一致を確認し、settleリトライループ・CRITICALアラート・benign cancel時の建玉確認分岐がいずれも稼働中コードに存在することを確認した。新たなデプロイ作業は発生していない。残タスクは、次回実際に板TPが発動した際のログで`SETTLE FAILED`のCRITICALが出ていないことの動作確認のみとなった（3.4.47・9章）。

**v81での変更点**: Instagram/Threadsへの投稿を、これまでの「Telegramに配信された内容を手動で投稿する」運用から、Graph API経由の自動投稿へ切り替えた（詳細は4.7.7）。Threads（テキスト）→Instagram Reels（動画）の順で実装し、承認ゲートなしで自動投稿→Telegramで結果確認→失敗時は手動リカバリという運用方針とした。Instagram Reelsの動画公開にはGraph APIの仕様上パブリックなHTTPS URLが必要なため、VPS上に専用サブドメイン（既存ドメインのサブドメイン、トークン付きURLのみ配信・2時間で自動削除）を新設して対応した。あわせて、Geminiで作成したオリジナル音源をリール動画に合成する機能（BGM未実装だった4.7.5からの変更）と、Threads/Instagramトークンの週次自動更新（60日で失効する長期トークンを毎週リフレッシュ）も実装した。

**v80での変更点**: ダッシュボードの当日損益とGMO取引履歴の合計が一致しない不具合を調査した結果、板監視TAKE_PROFIT決済（`_execute_real_board_take_profit_unlocked`）で、実約定価格の取得が実資金運用開始以来ずっと失敗し続けており、理論上のTP目標値（`pos.exit_price_target`）がpnlの記録に使われ続けていたことが判明した。

原因は`gmo_fetch_order_execution_fill()`が`/v1/executions`への照会失敗時に例外を無言で握りつぶし`None, None`を返す設計になっており、失敗理由が一切ログに残っていなかったこと。まず`gmo_fetch_order_execution_fill()`に、失敗理由（空レスポンス・価格欠損・手数料欠損・例外）を区別したログ出力と、初回失敗時のみ1.5秒待って1回リトライする処理を追加した（`_finalize_real_board_tp_settle_unlocked`、`_BOARD_TP_FILL_FETCH_RETRY_SEC`）。これにより実際のエラーが可視化され、根本原因はGMOの取引用APIキー（`GMO_API_KEY_TRADE`）に「約定情報取得」権限が付与されておらず、`/v1/executions`が常にERR-5012（Invalid API-KEY, IP, or permissions for action.）で拒否されていたことと確定した。GMO側の管理画面でこの権限を追加したことで解消し、権限修正後のTAKE_PROFIT決済では実約定価格の取得に成功している。

この権限不備は実資金運用開始からデプロイ（2026-08-04 00:01頃）まで存在していたと考えられるため、2026-08-01〜2026-08-05の期間で理論値フォールバックが発生した板監視TAKE_PROFIT決済55件について、GMOの`/v1/executions`から実約定価格を取得し直し、影響を確認した。55件全てで実約定価格の取得に成功し（GMO側の約定履歴は少なくとも約3.5日前まで遡って照会可能）、実際のpnl合計は記録済みpnl合計より+188.87円多かった（帳簿が利益を過小計上していた）。勝敗判定（pnlの符号）が変わるケースは無かった。

この55件について、`realtime_trading_log_2026-08-0[1-5].csv`のprice・pnl列を実約定値に修正し、修正行以降の`cumulative_pnl`列を日付順に再計算した（バックアップを`.bak_20260805`として保存）。`daily_history.jsonl`は影響を受けた2026-08-02〜2026-08-04の`realized_pnl`を更新した（2026-08-01は該当行が無いため新規作成せず据え置き）。`live_state.db`の`cumulative_pnl`は+188.87円分を反映したが、`daily_realized_pnl`（06:00起点の当日カウンタ）・`jpy_balance`・`total_gross_win`等は変更していない。当日カウンタを変更しなかったのは、55件のうち2026-08-05分（00:00〜05:39発生、権限修正前）が取引日としては2026-08-04扱いのため、修正時点の当日（2026-08-05、06:00以降）には影響する分が無かったため。実際の口座残高（`jpy_balance`）はGMO側の実残高に定期同期される値のため、今回の記録修正の影響を受けず、常に正しかった。

運用上の注意点として、エンジン稼働中に`live_state.db`を直接書き換えると、エンジン自身が毎秒DBを上書きするため変更が定着しないことが分かった。手動でのデータ修正が必要な場合は、ポジションがフラットであることを確認した上でエンジンを停止し、DB更新後に再起動して定着させる必要がある。

2026-08-04分の日次SNSレポート（Instagram・Threads）は、この修正が反映される前は未配信の状態だったため、修正後の`daily_history.jsonl`から改めて生成し直し、Telegramへ配信した。過去にすでに配信済みのSNS投稿（2026-08-03分以前）は訂正していない。

**v79での変更点**: SNS投稿（4.7.1）のAI考察文が、決済件数の多寡によらず常に「決済件数が多く、活発な一日でした」という同じ書き出しになる問題を修正した。原因はプロンプトに当日の生数値のみを渡しており、比較基準（直近実績）が無いままLLMに多寡を判断させていたこと、およびSNS_COMMENTARY_SYSTEMの【良い出力例】に、比較基準なしに件数の多寡を断定する例文が含まれていたことだった。

対応として、build_public_report.pyに`compute_recent_averages()`を新規追加し、対象取引日より前の取引日について、存在する分だけ（最大7日、日次履歴`log/daily_history.jsonl`の日付を基準に取得）決済件数・当日勝率・当日損益の平均を計算するようにした。このとき決済件数が0件の日は欠損等の異常とみなし、探索範囲（最大7日）自体は広げずにその中で平均計算から除外する。前日以前の対象日が1日も無い場合、または全て0件で除外された場合は`None`を返す。

あわせて`compare_to_recent_labels()`を新規追加し、当日実績と直近平均の実数比較はここで完結させ、以下のしきい値でラベル（文字列）に変換する。決済件数は直近平均比の差が30%未満で「平常通り」、30〜60%で「多め/少なめ」、60%以上で「多い/少ない」。当日勝率は直近平均との差が10ポイント未満で「横ばい」、10ポイント以上で「高め/低め」。当日損益は直近平均との差が「直近平均の絶対値の30%」と「100円」のうち大きい方を超えたら「改善傾向/悪化傾向」、それ以内は「横ばい」（改善/悪化は直近平均との相対的な傾向であり、当日損益そのものの符号とは別物）。

build_sns_report.py側では、`generate_ai_commentary()`に渡すuser_promptに、当日の生数値と直近平均の実数値は含めず、上記のラベルのみを事実として渡すようにした。比較対象となる直近実績が1日も無い場合（`compute_recent_averages()`が`None`）は、AI考察生成自体をスキップし、既存のフォールバック（考察文なしで基本項目のみ配信）に委ねる。SNS_COMMENTARY_SYSTEMの【良い出力例】は、比較基準なしに多寡を断定していた例文を削除し、ラベルの組み合わせ違いをカバーする複数パターンに差し替えた上で、「与えられたラベルに基づいてのみ多寡・傾向を表現すること」「毎回同じ書き出し・言い回しを使わないこと」を明記した。

生成後の安全策として`commentary_tone_matches_daily_pnl()`を新規追加し、当日損益の符号（プラス/マイナス）と生成文中のポジティブ/ネガティブ語が矛盾していないかをロジック（LLM再呼び出しなし）でチェックし、矛盾時はフォールバックする。実装初期は語彙リストに「改善」「悪化」を含めていたため、当日損益がマイナスでも「改善傾向」ラベルが正しく付いたケースを誤って矛盾判定してしまう不具合があったが、これらの語は当日損益の絶対的な符号ではなく直近平均比の傾向を表す語であるため、語彙リストから除外して修正済み。既存の文末不完全チェック（`is_complete_commentary`）とは併用する。

これらの直近平均・ラベルは内部入力としてのみ使用し、公開許可リスト（ALLOWED_KEYS）や投稿本文（見出し・数値項目・CTA・免責文・ハッシュタグ・冒頭フック）には一切変更を加えていない。リール動画（sns_reel_video.py）はこれらのキャプション定数を参照しない独立実装のため対象外。

**v78での変更点**: 本番運用移行（`trading_mode=real`）に伴い、SNS投稿テキスト（4.7.1）側にも実資金運用である旨を反映した。Instagramキャプションに残っていた「現在は仮想資金でのシミュレーション運用段階です。実資金での運用ではありません。」という免責文（`INSTAGRAM_CAPTION_DISCLAIMER`）を、「本アカウントは実資金でAIが自動売買を行った運用実績の記録です。投資助言・特定の売買を推奨するものではなく、将来の利益を保証するものでもありません。ご覧いただく際は自己判断でお願いします。売買ロジックの詳細は非公開としています。」に差し替えた。あわせて、Instagram側のハッシュタグ表示上限（5個）を超えていたため`INSTAGRAM_CAPTION_HASHTAGS`を「#BTC #AI自動売買 #仮想通貨 #トレード記録 #アルゴリズムトレード」の5個に絞った。Threads側にはこれまで免責文が存在しなかったため、新規に`THREADS_TEXT_DISCLAIMER`（「※実資金による運用記録です。投資助言ではなく、利益を保証するものではありません。」）を追加し、`compose_sns_message()`で本文・AI考察の後に付与するようにした。AI考察生成用のプロンプト入力からThreads免責文を除外するため`extract_threads_commentary()`を新規追加し、Instagram側の考察生成にThreads免責文が誤って流用されないようにした。見出し・数値項目・冒頭フック・CTA文言（「毎日この時間に更新しています。フォローして経過を見てもらえたら嬉しいです。」）には変更を加えていない。リール動画（4.7.5）はキャプション定数を参照しない独立実装のため対象外。SNS配信フォールバック（動画生成失敗時のテキストのみ配信）も同じ生成関数を経由するため、同一の文言で配信される。Cursorでの実装後、`--dry-run`でInstagram・Threads双方の出力テキストを目視確認し、免責文の差し替え・ハッシュタグ5個への変更を確認済み。

**v77での変更点**: 本番運用移行（`trading_mode=real`）に伴い、Instagramリール動画（4.7.5）を実資金運用であることが伝わる内容に更新した。(1) それまでシステム名・日付から成るヘッダーは指標カード画面にのみ表示され、冒頭フック区間には表示されていなかったため、動画の最初のフレームから最後まで常時表示される固定ヘッダーバーに変更し、冒頭フックの間も日付が見える状態にした。(2) 従来は仮想資金運用段階だったため意図的に含めていなかった稼働状況の断定表示について、実資金運用への移行を受けて、ヘッダー付近に「実資金で稼働している」という事実のみを伝える短いバッジ表示を追加した（`trading_mode`の値を見て表示可否を切り替える設計とし、「絶対に儲かる」「元本保証」等の断定・勧誘的な表現は含めない）。(3) 冒頭フックがテキスト静止のみだった点を改め、軽いフェードイン演出を追加した。(4) 冒頭フック3パターンのうち、v53で3行強調構成に改修済みだった1パターンに加え、未改修のまま残っていた残り2パターンについても同じ3行構成（中央行「BTC」のみ強調）に揃えたうえで、3パターンすべてを実資金である旨が伝わる文言（「AIが実資金で自動売買／BTC／24時間稼働中」「AIが実資金で運用中／BTC／24時間自動売買」「実資金でAIが売買／BTC／休みなく稼働中」）に更新した。Cursorへの実装指示・コミットは2026-08-03に完了しているが、本書時点ではVPS実機での動作確認（ピクセル解析・OCR等による見た目検証）は未実施であり、次回の動画生成時の確認待ちとした（9章参照）。

**v76での変更点**: 2026-08-03の本番インシデント（3.4.44直前の本文および3.4.45参照）の根本原因調査を受け、板TP（REAL-TP）決済経路の内部settle未完了問題に対応した。当日06:03:12、板TPのSL取消→成行`closeOrder`はGMO側で成功していたが、直後に発生したERR-5008（WebSocketManagerコールバック内の例外）により内部側のsettle/confirm処理が完走せず、その後のSLキャンセルリトライがすべてERR-5122（benign）としてsettleも成行決済も行わずreturnし続けたため、内部ポジションが約7分間宙に浮いたままreconciliation_mismatchでmanual_stopに至っていた。対応は2件。(1) benign cancel受領時に`fetch_open_positions()`で実際の建玉有無をpositionId突き合わせで確認し、フラットなら内部settle、建玉残存ならMARKET close続行、建玉取得自体が失敗した場合はCRITICALアラートして中断、という3分岐に変更した。(2) `closeOrder`受理後のconfirm/settle処理を、コールバック例外の影響を受けない独立したtry/exceptの最大3回リトライループに変更し、全試行失敗時のみCRITICAL（`SETTLE FAILED`）でTelegram通知する設計とした（`position.side is None`を判定に用いることで、リトライ中の二重settleを防止）。対象は板TP経路のみで、imbalance反転・メンテナンス強制・タイムアウト経由のキャンセル処理には変更を加えていない。テスト4本（benign+フラット→settle、benign+建玉残存→close続行、close後例外→リトライ完走、全試行失敗→CRITICAL）を含む`test_real_tp_sl.py`が9 passedで通過することを確認済み。行単位のコードレビューも実施し、意図通りの分岐・二重settle防止ロジックであることを確認した（3.4.47）。なお、CRITICAL到達までの最大所要時間が、リトライループの追加により即時から約10秒程度に伸びている点は許容範囲と判断した。反映はまだVPS実機には行っておらず、次回の板TP発生時のログでの動作確認待ち（9章参照）。

**v75での変更点**: 2026-08-04、`orphan-orders-check`から再度`[ALERT] orphan GMO active order(s) detected`および`[ALERT] wrapper exception / error=check_orphan_orders.py exit=1`が届いた事象を受け、2件の改善を実施した。(1) orphan判定を、同一`orderId`が5分間隔のチェックで2回連続して検知された場合にのみTelegramへ発報するよう変更した（`runtime/orphan_orders_state.json`に前回の候補`orderId`集合をatomic writeで保持）。GMO側からのactive order取得・orphan抽出条件自体（`live_state.db`の`entry/tp/sl_order_id`との突き合わせ）は変更していない。発注直後に`live_state.db`への反映が間に合わずorphanと誤検知するレースコンディション（8/3・8/4に発生、いずれも実際の建玉・注文には問題なし）への対策で、真のorphan発生時はアラートまで最大1チェック間隔（約5分）の遅延が生じる（3.4.45）。あわせて、3.4.44で「経過観察」としていたVPSのクロックドリフト仮説について、VPS上でSSH調査を実施し、NTP同期（`ntpsec`稼働、オフセット約+0.6〜1.2ms、GMO APIの`Date`ヘッダーと秒単位で一致）に問題が無いことを確認、常時ドリフトが原因である可能性は低いと判明した（ERR-5008の根本原因は依然未確定、9章に残タスクとして記録）。(2) 上記調査と並行して、朝5:55の日次レポートで夜間AI議論が2日連続（8/3・8/4）「記録なし」になっている事象を調査したところ、`ai-review.service`の`ExecStart`が`docker compose run --rm --profile manual ai_review ...`という構文になっており、VPSのDocker Compose v5.3.1では`--profile`が`run`サブコマンドの後ではなくグローバルオプションとして解釈されるため`unknown flag: --profile`で失敗し続けていたことが判明した。`--profile manual`を`run`より前に移動する形に修正し、リポジトリ側の`deploy/systemd/ai-review.service`とVPS実機側の`/etc/systemd/system/ai-review.service`両方に反映した（3.4.46）。修正後、当日分（`target_date=2026-08-03`）を手動実行して`status=applied`で正常終了することを確認済み。手動実行前に`pending_rollouts`が空（段階適用が進行中でない）ことを確認しており、これはcanary方式（5.4.2）が暦日ベースではなく`review_pipeline`の実行回数ベースで進む設計のため、ロールアウト進行中の日中手動実行は段階を意図せず進めてしまうリスクがあるという運用上の注意点として9章に記録した。

**v74での変更点**: VPS本番稼働3日目、`orphan-orders-check`から断続的に`[ALERT] wrapper exception / error=check_orphan_orders.py exit=1`が届く事象を調査した。ログを精査したところ3種類の原因が混在していた。(1) 8/1 21時台〜8/2 00時台の連続失敗（`ERR-5012`）は、既にv72で対応済みの`GMO_API_KEY_READONLY`デモキー問題そのものであり、`.env`修正が反映された時刻から正常化していたことをログで裏付けた。(2) 8/2 09:20の1件（`exit=127`）は、v72で修正済みの`python: command not found`が反映される直前のものと確認した。(3) 8/2 16:40・20:10の2件（`[Errno 2] No such file or directory: '.../monitor_heartbeats.json.tmp' -> '.../monitor_heartbeats.json'`）が未解決の新規事象として残り、複数の監視スクリプト（5分間隔の`crash-loop-check`・`orphan-orders-check`等）が固定名の一時ファイル（`monitor_heartbeats.json.tmp`）へ同時書き込みする際の競合と特定した。共通モジュール`scripts/monitor_heartbeat.py`を新設し、ファイルロック（Unix: `fcntl.flock`、Windows: `msvcrt.locking`）とプロセスID＋UUIDによる一意な一時ファイル名の組み合わせで解消した（3.4.44）。既存5スクリプト（`check_engine_crash_loop.py`・`check_orphan_orders.py`・`check_csv_db_consistency.py`・`check_trading_anomaly.py`・`check_engine_process.py`）すべての`_record_monitor_heartbeat()`をこの共通関数呼び出しに統一。なお、8/2 13:45・20:30に発生した`ERR-5008`（タイムスタンプ遅延）は、VPSの時刻同期（`timedatectl status`で`synchronized: yes`確認済み）に問題はなく、この競合によるI/O待ちが遠因だった可能性があるため、今回の修正で改善するか経過観察とする方針とした。

**v73での変更点**: VPS本番稼働開始後の初日（2026-08-02）に発生した5件の事象と対応を記録した。(1) 緊急停止後の「▶ 再開」ボタンがDocker/Linux環境ではフラグ削除のみでエンジンを再起動できない、v59以来の既知の構造的制約（717〜726行目参照）を解消した。`manual_stop.flag`検知後にプロセスを終了させず、決済完了後`engine_status=PAUSED`で同一プロセス内待機し、フラグ削除で自動的にWebSocket再接続・`RUNNING`復帰する設計に変更した（詳細は3.4.40）。(2) 内部日次レポート（`build_daily_report.py`、05:55、`daily_history.jsonl`への実現損益記録）に対応するsystemdタイマーがVPS構築時の9セットに含まれておらず、`daily-report.service`/`.timer`を新規作成・配備した（3.4.41）。この欠落により2026-08-01分の`daily_history.jsonl`記録が失われ、SNS配信・公開レポートが同日分のデータ欠如でエラーになった（8/1分は取引時間が短く重要度が低いため補完せず欠測日として扱う方針とした）。(3) `deploy/systemd/`配下の9つの`.service`ファイルに`User=takuya`の指定が無く、systemdがデフォルトの`root`として実行していたためhost側ログファイルの所有権が`root`になり、監視スクリプトの一部が書き込み権限エラーで失敗する事象が発覚し、全10セット（daily-report含む）に`User=takuya`・`Group=takuya`を追加した（3.4.42）。(4) `run_crash_loop_check.sh`・`run_orphan_orders_check.sh`が、v68時点の5本修正（4-2、9章）の対象に含まれておらず`python: command not found`で失敗していた事象を修正した（VPS側で直接`sed`修正後、Cursor側リポジトリにも反映）。(5) 起動時の`account integrity check`が実資金運用で`[ALERT]`となった事象を調査した結果、比較基準の`initial_jpy`（`config.json`の固定値、入金額を増やすたびに手動更新が必要）が実際の入金額とズレていたことが原因と判明し、real modeのみ比較基準を`daily_start_balance + daily_realized_pnl`（既存の日次損失上限判定と同じ、GMOの実残高に日次で自動同期する仕組み）に変更し、`initial_jpy`の手動管理を不要にした（3.4.43）。virtual側は従来の`initial_jpy + cumulative_pnl`基準のまま変更していない。

**v72での変更点**: 2026-08-01のVPS実機構築（ConoHa）完了・本番切替（`trading_mode=real`）完了を受け、9章の残タスクを一括で完了扱いに更新した。具体的には、(1) VPS実機構築・systemdタイマー9セット有効化・ネイティブ→Docker本番切替・SSHトンネル実地確認（いずれも4章末尾の「4.5.5 未検証の範囲」および9章）、(2) `run_crash_loop_check.sh`・`run_orphan_orders_check.sh`の2本に`run_public_report.sh`等の5本修正（v68）時点では未反映だった裸の`python`呼び出しが残っており、本番稼働直後に`python: command not found`で失敗した事象とその修正、(3) `.env`の`GMO_API_KEY_READONLY`が仮想資金用のデモキーのままだったため本番切替直後に`ERR-5012`が発生した事象とその修正（実資金用の参照専用キーを新規発行）、(4) 両compose同時起動時の二重発注防止（v70で未反映だった`VPS構築手順書.md`7章への追記）の反映、を記録した。あわせて、実機構築時に発生した多数のSSH・Docker関連のつまずき（ConoHaセキュリティグループ、socket activation、GitHub Deploy Key、Docker権限の再ログイン要件等）を反映し、`VPS構築手順書.md`をv11として全面刷新した（本書側の詳細な手順記載はそちらに委譲し、9章では事象と結果のみを記録する形にしている）。

**v71での変更点**: VPS実機構築を開始したところ、`VPS構築手順書.md`旧2.1節（監視系スクリプト用Python環境の構築）が、`cd /opt/gmo-btc-ws`から始まる内容にもかかわらず章としては3章（リポジトリのclone）より前の2章に置かれており、実行順と章番号が矛盾していたことが実機作業中に判明した。手順書側で旧2.1節を3.4節の後（新3.5節）に移動して解消し、本書内で「手順書2.1節」を参照していた残タスク欄（9章）の2箇所を「手順書3.5節」に更新した（v68・v69時点の変更履歴内の「2.1節」表記はその時点の節番号としてそのまま残している）。

**v70での変更点**: VPS移行準備の相談の過程で判明した2点を9章に追記した。(1) real-test環境がネイティブ本番運用と並行稼働していたことが発覚した事象（Windows Updateの強制再起動でDocker Desktopが再起動し、`restart: on-failure:5`により停止済みだったreal-testコンテナが再稼働）を受け、実際の対処（`docker compose down`での停止、GMO画面での手動決済）を実施済み。restartポリシーは現状の`on-failure:5`のまま変更しないことを決定した（起動時のaccount integrity checkが安全網として機能しているため）。この事象自体はVPS移行後にreal-testを本番と並行運用しない既存方針（9章「VPS移行後の並行運用方針」）で解消される。(2) `config/config.json`が`.gitignore`対象のためVPSへの`git clone`/`git pull`では引き継がれない点が判明し、本番移行時にネイティブ側の設定を手動でコピーする必要があることを9章に新規記録した（具体的な手順は`VPS構築手順書.md`3.4節に追記済み）。



**v69での変更点**: real-test環境でWindows Update起因の強制再起動後に発生した`real mode duplicate start blocked`（`existing_pid=1`）を調査・修正した（3.4.39）。原因は3.4.22の排他ロックがPID生存確認（`os.kill(pid,0)`）方式だったため、Dockerコンテナのメインプロセスが常にPID 1になる性質と、グレースフルシャットダウンを経ない停止でstaleロックが残る状況が重なると、新しいプロセス（同じくPID 1）が自分自身の生存確認をしてしまい常にブロックされる誤検知だった。`fcntl.flock(LOCK_EX | LOCK_NB)`によるOSレベルの排他ロックへ変更し、この種の誤検知を構造的に解消した。あわせて、real modeがDocker/Linux専用であることを明文化するため、Windowsネイティブでのreal mode起動を`_reject_real_mode_on_windows_or_exit()`で明示的に拒否する処理を追加した（`fcntl`がWindows非対応のため）。

**v68での変更点**: VPS移行前の残課題として9章に記録していた、監視・レポート系スクリプトのvenv参照方式を解消した（3.4.38）。`run_public_report.sh`・`run_sns_report.sh`をvenv（`.venv-host/bin/python`）明示参照に変更し、あわせてレビューの過程で、4.5.8で導入した`run_anomaly_check.sh`・`run_csv_consistency_check.sh`・`run_check_monitor_heartbeats.sh`の3本も**実際にはvenvを参照していなかったこと**が判明した（v67時点の記述はこの3本が既にvenv参照済みという誤った前提に基づいていた）。Cursorに5本まとめて`PYTHON_BIN`変数によるvenv明示参照への統一を依頼し、対応完了・構文確認済み（実機（VPS）での動作確認はVPS構築時に他セットとまとめて実施）。これにより9章の該当残タスク（4-2）は解消した。`VPS構築手順書.md`2.1節のチェックリストも同日付で更新済み。

**v67での変更点**: X投稿・SNS配信（4.6・4.7）のVPS/Linux版systemd配備を追加した（4.5.11）。`deploy/systemd/public-report.service`/`.timer`・`sns-report.service`/`.timer`と、対応するLinux版ラッパー`scripts/run_public_report.sh`・`scripts/run_sns_report.sh`を、既存の`anomaly-check.*`等と同じ構成（ロック・ログ出力・Telegram失敗通知）で新規作成した。改行コードはLFで、`.gitattributes`の既存設定（`*.service text eol=lf`・`*.timer text eol=lf`）の対象にも含めている。これにより`deploy/systemd/`配下の配備対象は7セットから**9セット**になった（9章の該当残タスクを更新）。あわせて、レビューの過程で`run_public_report.sh`・`run_sns_report.sh`が`python`コマンドをそのまま呼び出しており、4.5.8の3スクリプトと同様にVPSホスト側のvenv（`.venv-host`）を明示的に参照する書き方にはなっていない点が判明した。systemd経由の実行はシェルのvenv activateを経由しないため、VPS実機構築時に動作確認が必要な残課題として9章に記録した（ローカルでの構文確認・import確認のみ実施、実機未検証の点は4.5.10までの他5セットと同様）。

**v66での変更点**: VPS移行の前倒し（電気代・室温対策）に向け、本番切替前の残課題2件に対応した。(1) VPS本番用`docker-compose.yml`に、real-test環境（3.4.33）と同様のエンジンログ永続化・`PYTHONUNBUFFERED=1`設定を反映した。起動コマンドを`python -u scripts/run_engine_with_file_logs.py`に変更し、`environment`に`PYTHONUNBUFFERED=1`を追加。既存の`scripts/run_engine_with_file_logs.py`をそのまま使用でき、ネイティブ運用中のライブ状態（`live_state.db`等）とは無関係な使い捨ての検証用パス（`config-verify/`・`log-verify/`・`runtime-verify/`）でビルド・起動確認を行い、ログファイルの生成・標準出力バッファリングの解消（`Get-Content -Wait`でのリアルタイム追従）を確認した（文字化けの懸念があったが、ファイルはUTF-8で正しく書かれており表示側の問題と確認済み）。これにより9章の該当残タスクは解消した。(2) `deploy/systemd/`配下の全7セット（`ai-review`・`engine-restart`・`crash-loop-check`・`anomaly-check`・`csv-consistency-check`・`monitor-heartbeat-check`・`orphan-orders-check`）を確認し、3点を修正した。改行コードがCRLFのまま残っていた`ai-review.service`/`.timer`・`engine-restart.service`/`.timer`の4ファイルをLFに統一し、`.gitattributes`に`*.service text eol=lf`・`*.timer text eol=lf`を追加して再発防止（`.sh`向けの既存設定と同じ対応）。`ai-review.service`にsystemd既定のタイムアウト（90秒）ではAI夜間レビューのLLM呼び出しリトライを待ちきれない懸念があったため`TimeoutStartSec=0`を追加。`ai-review.service`の`ExecStart`（コンテナ内で評価される`ai_review/run_nightly_review.sh`という相対パス。`WorkingDirectory`はホスト側`docker compose`実行時の基準であり、この相対パス自体はホストパスの変更とは無関係だった）を、コンテナのWORKDIR（`/app`）を基準にした絶対パス`/app/ai_review/run_nightly_review.sh`に変更し、書き方を他6ファイルに揃えた。あわせて、9章の「Ubuntu VPS移行」残タスクに記載されていた配備対象「6セット」という記述が、2026-07-28追加の`orphan-orders-check`（4.5.10）を含めていない古い記述だったため、7セットに修正した。運用方針として、VPS本番稼働後はreal-test環境（実資金隔離テスト）を本番と並行運用しないこと（メリットがないため。real-testで見つかっていた不具合は今後本番側でその都度対応する）、仮想資金でのシミュレーション検証もシステムをシンプルに保つ目的でVPS/ローカルいずれでも本番と並行して残さない方針であることを決定し、9章に記録した。real-test停止確認の明文化は、`VPS構築手順書.md`側の本番切替手順（7章）に反映する。

**v65での変更点**: サーキットブレーカー（発注レート超過）の頻発事象を実データで調査し、根本原因を特定・対応した。real-test環境のログ・CSV分析により、imbalance反転によるキャンセル後、板に追従してわずかに価格が変わりながら短時間で再エントリーが繰り返され、既存の同一価格再エントリークールダウン（3.4.7）をすり抜けて6回以上の発注に達していたことが判明した。(1) imbalance反転によるキャンセル後のクールダウンを、同一価格ではなく**同一方向**で判定するように変更し、秒数も3秒から5秒へ延長した（`ENTRY_COOLDOWN_AFTER_CANCEL_SEC`）。timeout/deviationによるキャンセル後のクールダウン（同一方向かつ同一価格、300秒）・メンテナンス強制キャンセルの扱いには変更していない（3.4.35）。(2) real modeで指値発注が成功した時点（約定前）の記録が`realtime_trading_log`に残っておらず、未約定のまま短時間でキャンセルされたケースの事後調査ができなかった問題を解消するため、`reason=ENTRY_PENDING`として発注時点の記録を追加した（3.4.36）。あわせて、real modeのTAKE_PROFIT決済（板監視・成行決済）で記録される約定価格が常に目標値（`exit_price_target`）に固定され、成行注文の実際の約定価格（スリッページで目標値より良いことも悪いこともある）を反映していなかった問題を修正した。GMOの取引履歴との突き合わせでこのズレを確認し、手数料取得と同じ`/v1/executions`から数量加重平均の実約定価格も取得して反映するようにした（取得失敗時は目標値へフォールバック）。STOP_LOSS（WS約定通知ベースで元々実価格を使用）・virtual modeのシミュレーション計算・FORCE_CLOSE経路には変更を加えていない（3.4.37）。real-test環境で再ビルド・反映済み。

**v64での変更点**: 実資金隔離環境での運用上の問題2件に対応した。(1) 緊急停止後の再開手順（再開ボタンでは再開できずシステム再起動が必要）として`docker compose down`→リビルドを繰り返していたため、`docker logs`しか見ていなかったエンジンログがそのたびに失われ、サーキットブレーカー発動時刻の調査が不可能になっていた問題を解消した。`scripts/process_launcher.py`に`attach_engine_file_logs()`を追加し、標準出力・標準エラーをネイティブ運用と同じ命名（`engine_YYYY-MM-DD.log`/`.err.log`）でbind mount先（`log-real-test/`）へtee出力するようにした。Docker側のフォアグラウンド起動エントリポイントとして`scripts/run_engine_with_file_logs.py`を新設し、`docker-compose.real-test.yml`の起動コマンドをこれに切り替え、あわせて9章に低優先度で記録していた`PYTHONUNBUFFERED=1`未設定も解消した（3.4.33）。real-test環境で再ビルド・反映済み。なお、VPS本番用`docker-compose.yml`側にも同様の欠落（ログ永続化なし・`PYTHONUNBUFFERED`未設定）があることを確認したが、本番はまだ実機未検証のため今回は対応せず9章に記録するにとどめた。(2) GMOの実際の手数料がこの口座・銘柄では少額のため0円になっていた件（3.4.30）を受け、少額資金でテストしている仮想モード側の条件をこれに合わせるため、`virtual_trader.py`の`TAKER_FEE_RATE`定数を`0.0`に変更した（3.4.34）。この変更はCursorのレビューを経ない手動編集であり、既存テストへの影響未確認・将来資金規模が大きくなった際の手数料率再設定が必要になる点を9章に残タスクとして記録した。

**v63での変更点**: 実資金隔離環境（`docker-compose.real-test.yml`）のダッシュボードで「PENDING表示は残っているのに有効注文・建玉はGMO側に何もない、それにもかかわらず総資産金額が変動する」という実地の異常を調査し、根本原因が2件重なっていたことが判明、両方を修正した。(1) `_check_pending_fill()`で、板タッチによる仮想約定判定`filled`がreal modeのキャンセル判定（imbalance反転・価格乖離・タイムアウト）まで丸ごとgateしてしまい、価格が指値を大きく超えて動いた状態が続く限り、3.4.26のタイムアウトキャンセル機構を含む安全装置が一切機能しなくなっていた問題を修正した。仮想約定シミュレーションのスキップとキャンセル判定の評価を分離し、real modeでは`filled`の真偽に関わらず常にキャンセル条件を評価するようにした（3.4.31）。(2) 総資産計算（`portfolio_metrics.compute_total_assets`/`compute_total_assets_from_live_state`、および`VirtualTrader.unrealized_pnl`/`total_assets`）が`position_is_pending`を考慮せず、未約定のpending指値でも板価格の変動に応じた評価損益を総資産に加算してしまっていた問題を修正した。pending中は口座整合性チェック用の`comparable_equity_jpy`と同じく、現金残高（`jpy_balance`）のみを返すよう統一した（3.4.32）。両修正ともreal-test環境での実機確認済み。なお、実地調査の過程で「GMO画面から手動で注文を削除した際の一連の挙動（ERR-5122/5123のbenign処理によるキャンセル扱い）」は設計通り機能していることも確認できた。9章の「両compose同時起動時の二重発注防止」「孤児注文監視の心拍問題」「adopt後の同一tick内での連続close未対応」は今回未着手のため引き続き残タスクとして残っている。

**v62での変更点**: フェーズ2の残検証をまとめて実機確認・反映した（3.4.29）。SHORT側の一連フロー（エントリー・TP・SL、いずれもLONGと対称に正常動作）、再起動時のポジション状態復元（SHORTで確認）、`_force_close_real()`側のERR-5122/5123実機検証（LONGで確認、ただしエラー検知経路の厳密な確証は未確定）が完了し、9章の該当残タスクを解消した。あわせて、9章に保留中だった手数料の実測値反映も実施し（3.4.30）、ENTRY・SL決済・板監視TP・強制決済のいずれもGMOの実際の手数料を使うように変更した（取得失敗時は理論値フォールバック）。今回の過程で副次的に見つかった3件の低優先度の課題（pendingエントリーの起動時突き合わせが手薄・エンジンログのバッファリングによる可視性低下・ENTRY時`jpy_balance`反映が理論値のまま）は、いずれも実害なしと判断し9章に記録するにとどめた。

**v61での変更点**: フェーズ2の残検証の一つ、ERR-5122/ERR-5123の実機検証を実施した（3.4.21）。隔離環境でエントリー注文をGMO画面から手動キャンセル→緊急停止、という手順で`_force_cancel_maintenance()`側の挙動を実機確認し、ログ・ダッシュボード・Telegram通知いずれも「取消済として正しく処理・建玉誤採用なし」を示す結果となった。生の`message_code`自体はログに出ないため未確認だが、安全側の挙動は確認済み。同じERR-5122/5123を扱うもう一方の経路（`_force_close_real()`側のTP/SL取消、3.4.1）は引き続き未検証として残タスクに記録している。

**v60での変更点**: 3.4.28と同じ構造の問題が残っていた`_launch_nightly_review()`（AI夜間レビューの手動リトライボタン、6.4参照）にも同一パターンのOS判定を追加した。Windows以外では`run_nightly_review.ps1`の起動をスキップし、ログとダッシュボード画面双方に「この環境ではAI夜間レビューの手動起動は行えない、ai_reviewコンテナ側で別途実行する必要がある」旨を案内する。Windows環境での挙動・`run_nightly_review.ps1`自体・`_launch_ensure_engine_running()`は変更していない。これにより3.4.28で残タスクとして記録していた同種の未対応事項は解消し、9章の該当項目も削除した。

**v59での変更点**: フェーズ2の隔離環境（`docker-compose.real-test.yml`）で緊急停止後にダッシュボードの「▶ 再開」ボタンを押したところ`FileNotFoundError: 'powershell.exe'`が発生した事象を受け、`_launch_ensure_engine_running()`にOS判定を追加した。Windows以外（Docker/Linuxコンテナ内）では`ensure_engine_running.ps1`の起動をスキップし、ログとダッシュボード画面の双方に「フラグは削除したがエンジンの自動起動は行われないため、engineコンテナ側を別途再起動する必要がある」旨を案内するようにした（3.4.28）。`manual_stop.flag`削除処理自体やWindows環境での既存挙動、`ensure_engine_running.ps1`自体には変更を加えていない。同種の構造的な問題を抱える`_launch_nightly_review()`（AI夜間レビューの手動リトライボタン）は今回の対応範囲外のまま残っており、9章に残タスクとして記録した。

**v58での変更点**: 実運用で未約定の指値エントリーが約2時間放置された末に市況急変時に「飲まれる」形で約定した事象を受け、未約定エントリー指値のタイムアウトキャンセル機構を新設した。(1) 経過時間（プロファイル別、初期値60分）OR価格乖離（プロファイルの`stop_loss_pct`の50%）のいずれかを満たしたら、既存の共通キャンセル経路（imbalance反転・メンテナンス強制と同一）に合流させてキャンセルする方式で実装（3.4.26）。imbalance反転由来のキャンセルは従来通り3秒クールダウンを維持する一方、タイムアウト由来のキャンセルには専用の5分クールダウンを新設し、陳腐化したシグナルでの即時再発注ループを防いでいる。`realtime_trading_log`にキャンセル理由・条件成立フラグ・経過時間・乖離率のカラムを追加したが、AI夜間議論への集計対象には含めていない（今回はまず固定値で導入し、ログでの効果検証を優先する段階的方針のため）。(2) 実装直後に判明した副作用として、エンジン再起動時に未約定指値の発注時刻が`datetime.now()`で引き直され、時間タイムアウト条件が実質無効化される問題を修正した。`live_state.db`に発注時刻を永続化し、値が欠損／破損している場合は現在時刻へのフォールバックはせず時間条件のみをスキップする（乖離条件・imbalance反転条件は影響を受けず機能する）設計とした（3.4.27）。

**v57での変更点**: フェーズ2の隔離テスト環境で緊急停止(manual_stop.flag)を実行した際、エントリー中の指値注文がGMO側にキャンセルされずに残る実害（孤児注文）が発生したことを受け、Cursorによるコードレビューを実施し、判明した問題を優先度順に修正した（2026-07-27〜28）。(1) ポジション保有中は含み損益分で口座整合性チェックが誤検知しうる問題を修正（3.4.20）。(2) 緊急停止時、未約定(pending)のエントリー注文が決済処理に回され取消処理がされていなかった今回の実害そのものを修正（3.4.21）。(3) real-test環境と将来の本番環境が同じ発注用APIキーを共有する構成のため、多重起動排他ロックを追加（3.4.22）。(4) SL約定後・強制決済後にequity・累積損益(KPI)が同期されていなかった箇所を修正（3.4.23）。(5) 資金補正スクリプト`repair_live_state_equity.py`をreal modeのLONG会計（3.4.18）に対応させ、確認フラグなしでの誤適用を防止（3.4.24）。(6) 孤児注文監視(`check_orphan_orders.py`)をVPS/Linux本番向けに配備し、他の監視スクリプトと同じ死活監視の対象に追加（3.4.25、4.5.10）。レビューで指摘されたその他の項目（両compose同時起動の運用手順への反映、config読み取り失敗時の心拍問題等）は9章に残タスクとして記録した。

**v56での変更点**: フェーズ2（隔離テスト環境`docker-compose.real-test.yml`での実弾疎通確認）を開始し、初めてGMOの実口座にコードを通したことで顕在化した不具合を集中的に修正した（2026-07-26〜27）。(1) 認証・シンボル関連のエラー（署名対象へのクエリパラメータ混入`ERR-5010`、レバレッジシンボルの誤り`ERR-5106`、権限不足`ERR-201`等）を解消し起動可能な状態にした（3.4.16）。(2) 決済方式を抜本的に見直し、「反対売買では決済されず別建玉になる」実弾検証結果を受け、SLのみ`gmo_close_order`でGMO側に常設し、TPは板監視で到達検知後に「SLキャンセル→成行決済」で処理する設計に変更、`PositionState`への`position_id`保持・起動時突き合わせロジックもこれに追従させた（3.4.17）。(3) LONG側の内部残高会計がSHORT側と非対称で、エントリー・キャンセル・決済・強制決済等の各経路で想定元本の二重計上・計上漏れが多数発生していたのを、real modeのLONGでは手数料のみを増減させSHORTと同じ会計方式に統一した（3.4.18）。(4) `fetch_real_account_state()`の参照先をレバレッジの実際の余力`GET /v1/account/margin`に切り替え、整合性チェック用の時価評価総額は別途取得する設計に変更した（3.4.19）。あわせてreal-test専用インフラ（隔離docker compose定義・専用ダッシュボード・`initial_jpy`設定等）を追加した（4.5.9）。フェーズ2は引き続き稼働継続中で、ERR-5122/ERR-5123の実機検証・SHORT側の一連フロー確認・原因不明のエンジン停止調査が残タスクとして残っている（9章）。

**v54での変更点**: 夜間AI議論（`review_pipeline.py`）のLLM呼び出し失敗まわりを見直した。エラー種別を`classify_llm_error_kind()`で統一分類（タイムアウト／混雑／クォータ超過／その他）し、503等の一時障害・タイムアウトは30秒間隔で最大3回まで再試行、429（日次クォータ超過）は再試行せず即座に失敗させる（詳細は5.4.1）。あわせて、Proposer/Skeptic/Moderatorの呼び出し失敗時に深夜リアルタイムでTelegram通知していたものを廃止し（5.7）、代わりに内部日次レポート（`build_daily_report.py`、4.7.3）へ前夜のAI議論の成否・失敗種別・設定変更概要を追記するようにした。エンジン停止・サーキットブレーカー等、他の既存アラートは変更していない。242件パス。

**v53での変更点**: Instagramリール動画（4.7.5）の冒頭フックを2行から3行構成に変更し、「BTC」の行のみ他行より大きく強調表示するようにした。あわせて、通常の`--dry-run`では当日の実データがプラスの場合マイナス側の配色を目視確認できないため、QA専用の`--sample-data`オプションを追加し、当日損益をマイナスにしたサンプル値で動画を生成できるようにした（本番の動画生成フローには影響しない）。

**v52での変更点**: Instagramリール動画（4.7.5）の見た目を全面改修。中央揃えの単調な文字表示から、項目名左寄せ・数値右寄せのカード型レイアウトに変更し、数値は等幅フォント＋符号による色分け（プラス緑／マイナス赤）、単位（円・%・日・件・回）は数字より小さいサイズで分離描画するようにした。背景も純黒からダークネイビーに変更し、画面遷移は暗転ではなくフェードイン＋カウントアップに変更。稼働状況を断定する表示（LIVE/RUNNING等）は意図的に含めていない（詳細は4.7.5参照）。

**v51での変更点**: SNS拡張ステップ2（Instagramリール向け動画自動生成）を実装（4.7.5）。固定フックテキスト（LLM不使用）＋許可リスト指標のアニメーション表示、無音縦型動画、SNS用Botでの動画配信、フォールバックまで完了。`ai_review/telegram_notifier.py`と`scripts/telegram_notifier.py`の二重メンテナンスを解消し、`scripts/telegram_notifier.py`に一本化（`ai_review`側は互換シム化）。

---

## 1. システム概要

GMOコインのBTC/JPY板情報（Public WebSocket）をもとに、指値（Maker）エントリー・利確/損切りを自動判定する**仮想（シミュレーション）トレードシステム**。実際の資金は動かさず、5万円の仮想資金でロジックを検証する段階にある。

### 1.1 全体構成

```
gmo-btc-ws/                        ← プロジェクトルート（Gitリポジトリ、GitHub: festallypicco/gmo-btc-ws）
├── .git/                          Gitリポジトリ本体（2026-07-10導入）
├── .gitignore                     環境固有ファイル（config.json, runtime/配下, log/等）の除外設定
├── Dockerfile                     Dockerイメージ設計図（2026-07-11導入）
├── .dockerignore                  イメージに含めないファイルの指定（.gitignoreと同じ考え方）
├── docker-compose.test.yml        ローカル隔離テスト用のcompose定義（本番用ではない、2026-07-12にruntime/共有方式へ修正、2026-07-14に統合テスト完了・restart: on-failure:5に変更）
├── docker-compose.real-test.yml   real mode隔離テスト用のcompose定義（2026-07-26新設。config/runtime/logを専用パスに分離し、docker-compose.test.yml・ネイティブ運用と非干渉。詳細は4.5.9）
├── docker-compose.yml             本番用compose定義（engine/dashboard/ai_reviewの3サービス、2026-07-12新規作成。2026-07-14にengine/dashboardのrestartをon-failure:5に変更。VPS未構築のため実機未検証）
├── .env                           GMO_API_KEY/GMO_API_SECRET（2026-07-12新規作成、Git管理対象外）
├── .env.example                   上記のプレースホルダー版（Git管理対象）
├── trading_engine.py              売買エンジン本体（常駐プロセス）
├── run_dashboard.bat              ダッシュボード起動（エンジン起動確認込み）
│
├── runtime/                       実行時状態ファイル専用フォルダ（2026-07-12新設、詳細は4.5節）
│   ├── .gitkeep                   空でもディレクトリをGit管理下に残すためのダミーファイル
│   ├── manual_stop.flag           緊急停止フラグ（存在する場合のみ）
│   ├── live_state.db              エンジンの最新状態（SQLite、1秒毎更新）
│   ├── trading_engine.pid         実行中エンジンのPID記録
│   ├── anomaly_state.json         異常検知（check_trading_anomaly.py）の前回チェック時点の状態
│   ├── csv_db_consistency_state.json  CSV/DB整合性チェック（check_csv_db_consistency.py）の前回チェック時点の状態
│   ├── crash_loop_notified_<service>.flag   クラッシュループ通知の重複防止フラグ（存在する場合のみ、2026-07-14新設、詳細は4.5.6）
│   ├── monitor_heartbeats.json    監視系スクリプト3種の最終正常終了時刻（2026-07-15新設、詳細は4.1・4.3・9章）
│   └── heartbeat_stale_notified_<script>.flag  死活監視アラートの重複防止フラグ（存在する場合のみ、2026-07-15新設）
│
├── deploy/                        VPS配備用テンプレート（2026-07-12新設、VPS構築時にコピーして使用）
│   └── systemd/
│       ├── ai-review.service      AI夜間レビューのoneshotサービス定義
│       └── ai-review.timer        毎日03:30起動のsystemdタイマー定義
│
├── btc_trading_tool/              コアロジック（モジュール群）
│   ├── strategy_logic.py          売買シグナル判定（純粋関数、外部I/Oなし）
│   ├── virtual_trader.py          ポジション管理・損益計算・KPI集計・実口座整合性チェック・同一価格再エントリークールダウン（3.4.7）
│   ├── websocket_manager.py       GMO WebSocket接続・自動再接続（Public、`WebSocketManager`）。`PrivateWebSocketManager`（約定通知、real mode専用、詳細は3.4.11）も同ファイルに追加（2026-07-15）
│   ├── profile_config.py          時間帯プロファイルの純粋ロジック
│   ├── config_manager.py          config.json読み込み・マイグレーション・バリデーション
│   ├── portfolio_metrics.py       総資産計算の共通モジュール（LONG/SHORT/FLATの計算式、2026-07-22新設。dashboard.py・build_daily_report.pyの両方から利用、詳細は4.6・7.24）
│   └── dashboard.py               Streamlit表示専用ビューア
│
├── config/
│   ├── config.json                現在の設定（時間帯プロファイル込み、pending_rollouts含む）
│   └── last_loaded_config.json    変更差分検出用キャッシュ
│
├── log/                           各種ログ（日付ごとに分割されるものが多い）。`daily_history.jsonl`は日付を含まない追記型ファイルで、`build_daily_report.py`の`append_daily_history()`が05:55に1日1行追記する（取引日・現金残高・日末総資産等）。`build_ai_review_summary.py`の集計や、`build_public_report.py`のドローダウン算出（4.6）でも参照される
│
├── scripts/                       運用インフラ（PowerShell / Python）
│   ├── engine_process_utils.ps1   プロセス検出・起動・健全性確認の共通関数
│   ├── ensure_engine_running.ps1  手動起動時の二重起動防止
│   ├── restart_engine.ps1         06:00の強制再起動（設定反映用）
│   ├── detect_orphan_engines.ps1  孤立プロセスの定期監視
│   ├── check_live_state.py        live_state.db健全性チェック（環境変数LIVE_DB経由、直接パス組み立てはしない）
│   ├── reset_trading_state.py     live_state.dbのリセット・アーカイブ。当日CSVはbackup_dirへコピーのみ（元ファイルはlog/直下に残置、2026-07-15にshutil.moveから変更、詳細は7.19）
│   ├── check_trading_anomaly.py   取引頻度・損益急変の1時間毎監視（Telegram通知）。実装済みだが仕様書9章に長らく「未実装」と誤記されていた（詳細は7.8・9章）。正常終了時に`runtime/monitor_heartbeats.json`へハートビートを記録（2026-07-15）
│   ├── check_engine_crash_loop.py Dockerコンテナのクラッシュループ検知（RestartCount>=5でTelegram通知、2026-07-14新規、詳細は4.5.6）。正常終了時に`runtime/monitor_heartbeats.json`へハートビートを記録（2026-07-15）
│   ├── check_csv_db_consistency.py CSVとlive_state.dbの整合性チェック（3.4.9参照、2026-07-14新規）。正常終了時に`runtime/monitor_heartbeats.json`へハートビートを記録（2026-07-15）
│   ├── check_monitor_heartbeats.py 上記3スクリプトの死活監視（2026-07-15新規）。`runtime/monitor_heartbeats.json`を読み、許容間隔を超えて更新が無ければTelegram通知（詳細は9章）
│   ├── run_crash_loop_check.ps1   上記の薄いラッパー（本番docker-compose.ymlを対象、BTC_Crash_Loop_Checkから5分毎に実行）。catchブロックおよび`$LASTEXITCODE`非0時の両方でTelegram即時通知（`Send-WrapperTelegramAlert`、2026-07-15にsys.exit(1)経路も対応）
│   ├── run_csv_consistency_check.ps1  check_csv_db_consistency.pyの薄いラッパー（ロック・ログ・try/catch/finally、2026-07-14）。catchブロックおよびPython非0終了コード検知時の両方でTelegram即時通知（`Send-WrapperTelegramAlert`、2026-07-15にsys.exit(1)経路も対応）
│   ├── run_check_monitor_heartbeats.ps1  check_monitor_heartbeats.pyの薄いラッパー（ロック・ログ・try/catch/finally、2026-07-15新規、BTC_Monitor_Heartbeat_Checkから15分毎に実行）。Python非0終了コード検知時にTelegram即時通知（`Send-WrapperTelegramAlert`を新規追加、2026-07-15）。catchブロック（PowerShell側の例外）には未対応（9章参照）
│   ├── telegram_notifier.py       Telegram通知（`ai_review/.env`を参照、詳細は5.6）。check_engine_crash_loop.py・check_csv_db_consistency.py・check_monitor_heartbeats.pyも本ファイルを流用。2026-07-15に`scripts/.env`を廃止し`ai_review/.env`へ一本化した（旧9章の低優先度課題、解消済み）
│   ├── build_public_report.py     X向け公開日次レポートの集計・投稿（フェーズ1: テキストのみ、2026-07-22新規、詳細は4.6）。許可リスト（`ALLOWED_KEYS`）に定義された7項目のみを出力する設計
│   ├── run_public_report.ps1      build_public_report.pyの薄いラッパー（Task Scheduler用、2026-07-22新規。BTC_Public_Report_Postとしての登録は試験投稿期間終了後に実施予定、詳細は4.6・9章）
│   ├── requirements.txt           scripts/配下のPython依存関係（tweepy>=4.14.0、2026-07-22新規）
│   ├── repair_live_state_equity.py  ポジション未復元バグ（3.4.14）による資金欠損の一度きりの補正スクリプト（デフォルトdry-run、`--apply`で適用、2026-07-23新規）
│   ├── build_sns_report.py        Instagram/Threads向けSNS投稿用テキストの生成・LLM考察付与・専用Bot配信（4.7、2026-07-23新規）
│   ├── sns_reel_video.py          Instagramリール向け動画自動生成（PIL+imageio-ffmpeg、無音縦型、4.7.5、2026-07-24新規）
│   ├── check_engine_process.py    ネイティブ運用でのエンジン完全停止検知・自動復旧（3.4.15、2026-07-23新規）
│   └── run_check_engine_process.ps1  check_engine_process.pyのTask Scheduler用ラッパー（3.4.15、2026-07-23新規）
│
├── ai_review/                     AI議論による設定最適化パイプライン
│   ├── build_ai_review_summary.py 集計サマリー生成
│   ├── build_change_outcomes.py   設定変更の事後評価
│   ├── llm_clients.py             Groq/Gemini呼び出しラッパー
│   ├── prompts.py                 Proposer/Skeptic/Moderatorのプロンプト
│   ├── review_pipeline.py         議論オーケストレーション・ガードレール・外れ値検知/段階適用
│   ├── run_nightly_review.ps1     Windows用、03:30の夜間実行ラッパー（ローカルテスト環境では継続使用）
│   ├── run_nightly_review.sh      Linux用、run_nightly_review.ps1と同一ロジックのbash版（2026-07-12新規作成、VPS用）
│   ├── .env / .env.example        APIキー管理
│   ├── requirements.txt
│   └── README.md                  運用ドキュメント・月次チェックリスト
│
├── conftest.py                     pytestルートフィクスチャ（2026-07-22新規）。session終了時に本番取引CSVのsha256/mtimeが変化していないことを検証する（詳細は7.23）
│
└── （プロジェクトルート直下、フラット構成。2026-07-16以降に追加されたテストは`tests/`サブディレクトリに置く運用へ移行。詳細は4.4節末尾を参照）
    ├── test_trading_engine.py             発注レート制限のテスト
    ├── test_virtual_trader.py             実口座整合性チェックのテスト（2026-07-22、本番CSVを汚染しないよう`LOG_DIR`をtmp_pathへ差し替えるautouseフィクスチャを追加、詳細は7.23）
    ├── test_review_pipeline_rollout.py    外れ値検知・段階適用のテスト
    ├── test_maintenance_entry_block.py    メンテナンス時間帯のエントリーブロックのテスト（7.5参照）
    ├── test_config_manager.py             trading_modeバリデーションのテスト（3.4.10）
    ├── test_private_websocket_manager.py  PrivateWebSocketManagerのテスト（3.4.11）
    ├── test_force_close_real.py           _force_close_real()のテスト（3.4.12）
    ├── test_build_change_outcomes.py      設定変更の事後評価のテスト（5.2.4）
    └── check_gmo_api_connection*.py       GMO実API疎通確認用の単独実行スクリプト（一時的な調査用、常設運用スクリプトではない）

├── tests/                          2026-07-16以降に追加されたテスト（サブディレクトリ運用、上記フラット構成と併存）
│   ├── test_csv_db_consistency.py         CSVとlive_state.dbの整合性チェックのテスト（3.4.9）
│   ├── test_backtest_verifier.py          バックテスト検証ゲートのテスト（5.4.3）
│   ├── test_build_ai_review_summary_trades.py     約定データ集計のテスト（5.2.1）
│   ├── test_build_ai_review_summary_depth.py      板の厚み集計のテスト（5.2.2）
│   ├── test_build_ai_review_summary_volatility.py ボラティリティ集計のテスト（5.2.3）
│   ├── test_websocket_orderbook_depth.py  板の複数階層の厚み収集のテスト（3.3.2）
│   ├── test_market_snapshot_volatility.py ボラティリティ収集のテスト（3.3.3）
│   ├── test_proposer_prompt_size_guard.py Proposerプロンプトの肥大化監視のテスト（5.3）
│   ├── test_archive_old_logs.py           ログのzip圧縮アーカイブのテスト
│   ├── test_build_daily_report.py         日次サマリーTelegram通知のテスト（2026-07-22、total_assets_eodの記録を追加検証）
│   ├── test_build_public_report.py        X向け公開日次レポートのテスト（許可リスト検証・CSV集計・ドローダウン・CB判定・ドライラン、2026-07-22新規、詳細は4.6）
│   ├── test_production_data_integrity.py  本番取引CSVがテスト実行前後で変化しないことを検証するガードのテスト（2026-07-22新規、詳細は7.23）
│   └── test_portfolio_metrics.py          総資産計算（LONG/SHORT/FLAT）のテスト（2026-07-22新規、詳細は7.24）
```

Git管理対象外（`config/config.json`, `runtime/`配下, `log/`配下, `.env`系等）の詳細は4.4節を参照。テストファイル群は削除・移動せず、現状のプロジェクトルート直下フラット構成のまま維持する方針（回帰テストとしての価値を優先。将来`tests/`フォルダへの整理は別タスクとして9章で検討）。

### 1.2 1日の流れ

| 時刻 | 何が起きるか |
|---|---|
| 00:00 | ログファイルが日付で切り替わる（`log/realtime_trading_log_YYYY-MM-DD.csv`等）。エンジン自体は稼働継続中で、再起動は発生しない |
| 03:30 | `BTC_AI_Nightly_Review`タスク起動。AI議論パイプラインが前日ログを分析し、`config.json`を書き換える（安全なら） |
| 05:55〜06:30 | GMOコインのメンテナンス時間。新規エントリー停止・保有ポジション強制決済 |
| 06:00 | `BTC_Engine_Restart`タスク起動。エンジンを再起動し、新しい`config.json`を読み込む |
| 06:00〜翌05:55 | 通常稼働。1時間ごとに`BTC_Detect_Orphan_Engines`が孤立プロセスを監視。日付が変わる00:00をまたいでもエンジンは再起動しない（ログファイルの切り替えのみ） |

**注**: 稼働サイクルの起点は`06:00`のエンジン再起動であり、暦日（00:00）ではない。00:00はログファイル名が切り替わるだけの節目で、AI議論（03:30）やconfig反映（06:00）のタイミングとは独立している。

---

## 2. 時間帯プロファイル機構

### 2.1 概要
`config/config.json`は、1日を複数の時間帯（プロファイル）に分割し、それぞれ異なる`StrategyConfig`パラメータを持たせられる。

```json
{
  "version": "2026-07-04_09-30",
  "updated_reason": "introduce morning/day/night time-based profiles",
  "profiles": [
    {"name": "early_morning", "start_time": "00:00", "end_time": "08:00", "imbalance_entry_threshold": 0.62, "take_profit_pct": 0.0012, "stop_loss_pct": 0.0012, "daily_target_order_size_btc": null, "daily_target_order_size_reasoning": null, ...},
    {"name": "daytime",       "start_time": "08:00", "end_time": "16:00", "imbalance_entry_threshold": 0.55, "take_profit_pct": 0.0015, "stop_loss_pct": 0.0015, ...},
    {"name": "night",         "start_time": "16:00", "end_time": "24:00", "imbalance_entry_threshold": 0.58, "take_profit_pct": 0.0020, "stop_loss_pct": 0.0018, ...}
  ]
}
```

制約: プロファイルはギャップ・重複なく24時間をちょうど埋め尽くす必要がある（`validate_profiles()`が起動時に検証）。日付をまたぐプロファイル（例: 22:00-06:00）はサポートしない。

### 2.2 最重要の安全設計: 保有中ポジションの設定固定
時間帯の境界をまたいでも、**保有中のポジションはエントリー時点で有効だったプロファイルの設定値を決済まで使い続ける**。新しいプロファイルは次の新規エントリーから適用される。これにより、保有中に突然TP/SL幅が変わるという予期しない挙動を防いでいる（`virtual_trader.py`の`_locked_config`/`_locked_profile_name`）。

### 2.3 起動時バリデーション（フェイルセーフ）
`config.json`のプロファイル定義に不正がある場合（ギャップ・重複・必須フィールド欠落等）、`trading_engine.py`は**PIDファイル・live_state.dbを一切更新せずに即座に異常終了**する。これにより、`ensure_engine_running.ps1`/`restart_engine.ps1`の健全性チェックが正しく「起動失敗」を検知できる。

---

## 3. 売買エンジン（trading_engine.py）

### 3.1 役割
- `config/config.json`読み込み・マイグレーション・バリデーション
- `WebSocketManager` + `VirtualTrader`の起動
- 1秒ごとに`live_state.db`を更新
- 60秒ごとに`log/market_snapshot_YYYY-MM-DD.csv`を記録（取引の有無に関わらず、板の状態を定期記録）
- SIGINT/SIGTERM受信時に安全停止

### 3.2 メンテナンス対応
- GMOコインの**毎日05:55〜06:30**のメンテナンス時間帯: 新規エントリー停止のみ（`_is_daily_maintenance_window()`、`_DAILY_MAINTENANCE_START=05:55` / `_DAILY_MAINTENANCE_END=06:30`、半開区間`start <= now < end`）。**保有中ポジションの強制決済は行わない**（2026-08-07確認・方針決定、詳細は7.26参照）。状態遷移時（メンテナンス突入・解除）に`[WARNING] [Maintenance] 定期メンテナンスのため待機中`等のログを出すのみで、`_force_close_real()`はこの経路からは呼ばれない
- **毎週土曜09:00〜11:00の定期メンテナンス**（プレメンテ枠として08:55〜09:00を含む）にも対応するロジックが実装済み（`_is_weekly_maintenance_window()`、`_WEEKLY_MAINTENANCE_*`）。プレメンテ枠中（`in_pre`）かつ`maintenance_pre_action=close`設定時は`_force_close_real()`による強制決済が実行される（日次とは異なる経路）
- `manual_stop.flag`検知時も`_force_close_real()`による強制決済が実行される（3.4.1、5.3節参照）
- `_is_entry_blocked()`は日次・週次いずれの判定もOR条件で新規エントリーをブロックする

**インシデント履歴**: 2026-07当初、日次判定のみ実装から漏れており、毎朝05:55〜06:30にENTRYが発生し続けていた。詳細は7.5参照。2026-08-07、日次メンテナンス時間帯にポジションが強制決済されず持ち越された事象を調査した結果、上記の通り設計上そもそも日次窓には強制決済の実行経路が存在しないことが判明し、これを踏襲する方針とした。詳細は7.26参照。

### 3.3 取引ログ（realtime_trading_log_YYYY-MM-DD.csv）
主なカラム: `timestamp, side, order_type, reason, price, size, fee, pnl, duration_sec, cumulative_pnl, imbalance, spread_pct, best_bid_size, best_ask_size, cfg_*, config_version, profile_name`

`cfg_*`には`cfg_daily_target_order_size_btc`を含む（発注時点での設定値。未設定時は空欄）。

`reason`の種類: `ENTRY` / `TAKE_PROFIT` / `STOP_LOSS` / `CANCEL_ORDER` / `FORCE_CANCEL_MAINTENANCE` / `FORCE_CLOSE_MAINTENANCE`

### 3.3.1 約定データの収集（trades購読、2026-07-17実装）

**目的**: 板情報（best bid/ask のサイズ）だけでは実際に約定が伴っているかが分からないため、GMOコインのPublic WebSocket約定通知（`trades`チャンネル、symbol=`BTC`）を追加購読し、直近の買い成行・売り成行の出来高を記録できるようにした。現時点ではログ記録のみで、AI夜間議論（5章）への組み込みは別タスク（データ蓄積後に対応予定）。

**実装（`websocket_manager.py`）**:
- 板情報の購読・処理経路とは別に、`_on_trades_message`で約定通知を受信する専用コールバックを新設
- 受信した価格・数量・BUY/SELL・時刻を`_trade_buffer`に蓄積（専用ロックで保護、板情報側の処理とは独立）
- `consume_trade_window_stats()`で件数・買い出来高合計・売り出来高合計を集計し、呼び出し時にバッファをクリアする（読み取りと同時にリセットする設計）
- 板→約定の連続`subscribe`で`ERR-5003`が発生することがあるため、両チャンネルの購読リクエストの間に2秒待機を入れている

**market_snapshotへの列追加（`trading_engine.py`の`MarketSnapshotLogger`）**:
- 新規列: `trade_count`（60秒間の約定件数）, `buy_volume`（買い成行出来高合計）, `sell_volume`（売り成行出来高合計）
- 60秒ごとの書き込み時に`consume_trade_window_stats()`を呼んで集計→クリア。約定が1件もなかった区間も`0`を記録する（欠損扱いにしない）
- 既存列（`imbalance`・`spread_pct`等）・売買シグナル判定（`strategy_logic.py`）には未着手。あくまでログ記録の拡張のみ

**ローカル確認**: 実WSで板・約定を同時購読しCSVへの書き込みを確認済み（例: `trade_count=2 buy=0.00033 sell=0.00033`）。ヘッダへの3列追加、約定ゼロ区間での`0`記録も確認済み。実機での全カラムは以下の通り:
`timestamp, best_bid_price, best_bid_size, best_ask_price, best_ask_size, mid_price, imbalance, spread_pct, trade_count, buy_volume, sell_volume`

**運用上の注意（未対応・手動での回避が必要）**: 当日分の`market_snapshot_YYYY-MM-DD.csv`が実装前の旧ヘッダ（列3つ少ない状態）で既に存在している場合、新ヘッダのプロセスがそこに追記すると列がずれる。自動マイグレーションは実装していないため、エンジン再起動前に当日ファイルを退避・リネームするか、日付が変わるのを待ってから反映すること。

### 3.3.2 板の複数階層の厚み収集（depth5、2026-07-17実装）

**目的**: 既存の`imbalance`は最良気配（best bid/ask）のサイズのみを見ており、板の厚み全体（2階層目以降）を反映していない。より広い階層の厚みを判断材料として記録できるようにした。現時点ではログ記録のみで、AI夜間議論（5章）への組み込みは別タスク。

**事前調査結果**: GMOコインPublic WebSocketの`orderbooks`メッセージには、実測で買い/売り各30階層分の価格・サイズが元々含まれていることを確認した。そのため新規チャンネル購読は不要で、既存の板情報受信メッセージから読み取る階層を広げるだけで実装できた。

**実装（`websocket_manager.py`）**:
- 板情報受信時に、買い側・売り側それぞれ浅い方から5階層分のsizeを合計する`latest_depth_stats`プロパティを追加
- 板が5階層未満の場合はある分だけで計算する（エラーにしない）
- 既存の`OrderbookSnapshot`（最良気配のみ）・板情報の売買コールバックは変更なし

**market_snapshotへの列追加（`trading_engine.py`の`MarketSnapshotLogger`）**:
- 新規列（末尾に追加）: `bid_depth5_size`（買い5階層のsize合計）, `ask_depth5_size`（売り5階層のsize合計）, `depth_imbalance`（買い合計 ÷ (買い合計+売り合計)。分母0の場合は空欄）
- 既存の`imbalance`（最良気配ベース）・`spread_pct`の計算方法は変更・削除していない。今回追加するのは新しい列のみ
- `strategy_logic.py`（売買シグナル判定）・エントリー/決済ロジックには未着手

**テスト**: ユニットテストで5階層未満・不正size・分母0の4パターンを確認。実WS + CSV書き込みで新3列の記録も確認済み。

**運用上の注意**: 3.3.1のtrades列追加時と同様、当日分の`market_snapshot_YYYY-MM-DD.csv`が旧ヘッダのまま残っていると追記時に列がずれる。本番反映時は、エンジン再起動前に当日ファイルを退避するか、日付が変わって新ファイルが作られるのを待つこと。

### 3.3.3 ボラティリティの収集（volatility_5min_range_pct、2026-07-17実装）

**目的**: 現在は時間帯だけでプロファイルを分けており、同じ時間帯でもボラティリティ（値動きの荒さ）が高い日/低い日を区別できていない。市場のボラティリティ状況を判断材料として記録できるようにした。現時点ではログ記録のみで、AI夜間議論（5章）への組み込みは別タスク。

**実装（`trading_engine.py`の`MarketSnapshotLogger`）**:
- 直近5件の`mid_price`を保持するローリングバッファ（`deque(maxlen=5)`）を新設。trades（3.3.1）・depth5（3.3.2）のバッファとは独立
- 新規列`volatility_5min_range_pct`: バッファ内`mid_price`の最大値と最小値の差を現在の`mid_price`で割った値（`spread_pct`と同じ比率形式、例: 0.03 = 3%）
- バッファが5件未満（エンジン起動直後等）の場合、および`mid_price`が0の場合は空欄（`null`）で記録し、少ない件数での計算や0埋めは行わない
- 既存の`imbalance`・`spread_pct`等の計算方法・`strategy_logic.py`（売買シグナル判定）・エントリー/決済ロジックには未着手

**テスト**: バッファ不足時のnull・5件到達時の計算・`mid_price=0`時のnull・CSVへの新列書き込みの4パターンを確認済み。

**運用上の注意**: 3.3.1・3.3.2と同様、当日分の`market_snapshot_YYYY-MM-DD.csv`が旧ヘッダのまま残っていると追記時に列がずれる。本番反映時は、エンジン再起動前に当日ファイルを退避するか、日付が変わって新ファイルが作られるのを待つこと。

### 3.4 安全装置（実資金投入前の最終防衛ライン、2026-07-10実装）

AIによる根拠の吟味を一切経由せず、条件を満たしたら機械的に`manual_stop.flag`を生成して緊急停止する2つの仕組み。7.3（動的サイジング複利暴走）のような原因不明のバグが実資金運用中に再発した場合、口座資金が枯渇する前に食い止める最後の砦として位置づける。

#### 3.4.1 注文レートのハードリミット（`trading_engine.py`）
- `check_order_rate_limit()` / `record_order_event()`: 直近60秒間の発注回数を監視し、`order_rate_limit_per_minute`（config.json、デフォルト5）を超えたら即座に`manual_stop.flag`を生成する
- エントリー発注処理の直前に判定し、超過時はその発注自体もスキップする（発注してからカウントするのではなく、ブロックされた試行はカウントに含めない）

#### 3.4.2 実口座整合性チェック（`virtual_trader.py`）
- `fetch_real_account_state()`: GMOコインPrivate APIから実際の建玉・口座残高を取得する（発注系キー`GMO_API_KEY_TRADE`/参照専用キー`GMO_API_KEY_READONLY`への分離後の詳細、および参照先エンドポイントの変更は3.4.19参照。未設定時は警告ログのみで停止しない）
- `compare_with_internal_state()`: `live_state.db`内のシステム認識（建玉・残高）と実口座を比較し、建玉差`reconciliation_tolerance_btc`（デフォルト0.0005 BTC）・残高差`reconciliation_tolerance_jpy`（デフォルト100円）のいずれかを超えたらmismatch扱いにする
  - `0.0005 BTC`はGMOの最低発注単位（0.001 BTC）の半分。本物のズレ（0.001以上）は確実に検知しつつ、float演算の丸め誤差はノイズとして許容する閾値として設定
- `run_reconciliation_check()`: `reconciliation_interval_minutes`（デフォルト5分）ごとに実行。1回だけの不一致はAPI瞬断等のノイズの可能性があるため確定させず、**2回連続で不一致だった場合のみ**`manual_stop.flag`を生成する

#### 3.4.3 `manual_stop.flag`との連携
上記いずれの安全装置も、6.7で説明した既存の緊急停止の仕組み（`manual_stop.flag`検知→`_force_close_maintenance()`による強制決済→`engine_status`遷移）にそのまま乗る形で実装されている。新たな停止経路は作らず、既存の1つの経路に集約している。

`manual_stop.flag`のパスは`trading_engine.py`・`virtual_trader.py`（`btc_trading_tool/`配下）・`dashboard.py`（`btc_trading_tool/`配下）の3箇所で組み立てているが、いずれも最終的に同一の`runtime/manual_stop.flag`を指すことを実機確認済み（緊急停止ボタン押下→ログでの検知を確認）。2026-07-12の案B実装（4.5節）により、以前はプロジェクトルート直下だったパスを`runtime/`配下へ変更した。

**自動発動時のTelegram通知（2026-07-14実装）**: 7.13「サーキットブレーカー発動の通知にすぐ気づけず、再開が遅れる」を受け、3.4.1・3.4.2・3.4.8の**自動発動時**に限り、`trading_engine.py`の`_trigger_safety_stop(reason, details=None)`からTelegram通知を送るようにした。

- `reason`（`"order_rate_limit"` / `"reconciliation_mismatch"` / `"daily_loss_limit"`）と、判定に使った具体的な値（`details`）を呼び出し元がそれぞれ渡し、通知本文に含める
- 通知基盤は新規実装せず、`scripts/telegram_notifier.py`の`send_telegram_message()`を流用（`check_engine_crash_loop.py`と同じ方針。`ai_review/telegram_notifier.py`には触れていない）
- **重複防止**: `manual_stop.flag`が既に存在する場合は、フラグ作成・通知のどちらも行わず即座にreturnする。`_trigger_safety_stop`が複数回呼ばれても、実際にフラグを新規作成した時（＝発動の瞬間）のみ通知が送られる
- 通知送信の失敗は`try`/`except`で握りつぶし、フラグ作成や緊急停止処理自体には影響しない設計とした
- **対象外**: 6.7のダッシュボード「緊急停止」ボタンによる手動停止は、`_trigger_safety_stop`を経由しない別経路（ボタン押下でダッシュボードが直接`manual_stop.flag`を作成）のため、今回の変更の対象外。人間が能動的に操作した結果であり通知は不要という判断で、Telegram通知は行われない（従来通り）

#### 3.4.4 config.jsonへの追加項目
```json
{
  "order_rate_limit_per_minute": 5,
  "reconciliation_interval_minutes": 5,
  "reconciliation_tolerance_btc": 0.0005,
  "reconciliation_tolerance_jpy": 100
}
```
（`config_manager.py`の`apply_engine_safety_defaults()`で、値が存在しない場合はデフォルト値を自動補完する）

#### 3.4.5 GMO実APIとの疎通確認（2026-07-10実施）
本番移行前に、ダミー運用中の口座（残高0円）で`fetch_real_account_state()`の疎通・レスポンス形式を確認した。

- **注意点**: GMOコインPrivate APIのベースURLは`https://api.coin.z.com/private`であり、`/private`セグメントが必要（`https://api.coin.z.com`直下ではない）。調査用に作成した簡易スクリプトで一度この`/private`抜けにより404エラーを踏んだが、本体コード（`fetch_real_account_state()`）は元から正しく実装されていたことを確認済み
- **APIキー権限**: 現時点（参照確認のみ）では「資産残高を取得」「建玉一覧を取得」の2つのみを付与。発注系権限（注文・決済注文・注文キャンセル等）は、実資金投入フェーズで別のAPIキーを新規発行する際に追加する方針とし、参照専用キーとは用途を分離する
- 残高0円・建玉なしの状態で、認証・署名生成・レスポンス構造（`status`/`data`形式）が想定通りであることを確認済み。建玉がある状態でのside/size変換ロジックの確認は、少額の実弾投入後に改めて行う

#### 3.4.6 テスト
`test_trading_engine.py`（4件）: レート制限の境界値（上限以下/超過/ちょうど上限/60秒超過イベントの除外）
`test_virtual_trader.py`（10件）: `compare_with_internal_state()`の境界値（許容内/建玉差超過/残高差超過/ちょうど許容値/mismatch dictの全フィールド）、`run_reconciliation_check()`の状態遷移（一致/初回不一致/2回連続不一致/再取得で一致に復帰/API例外時の非伝播）

#### 3.4.7 同一価格キャンセル後の再エントリークールダウン（`virtual_trader.py`、2026-07-14実装）

**背景**: 板の`imbalance`（買い圧力）は1ティックごとに再計算されるため、値がエントリー閾値（例: `daytime`プロファイルの0.55）付近にあるとき、価格が全く動いていなくても閾値をまたいで頻繁に往復することがある。この往復のたびに「エントリー→（閾値割れで）キャンセル→（閾値回復で）再エントリー」を繰り返し、正常な挙動であるにもかかわらず3.4.1のサーキットブレーカー（60秒あたり5発注）を頻発させる原因になっていた（7.13参照）。

**実装内容**:
- BUY/SELL各方向ごとに、直近キャンセルした価格・時刻をインメモリで保持する（`_last_cancel_by_side`。DB・ファイルへの永続化はしない。プロセス再起動でリセットされる想定）
- `_cancel_order()`実行時に、該当方向の直近キャンセル情報を更新する
- 新規エントリー直前に、**同一方向・同一価格・経過時間が`ENTRY_COOLDOWN_AFTER_CANCEL_SEC`（固定値3秒、モジュール内定数。2026-07-14に2秒で導入、2026-07-15に3秒へ変更）未満**の場合、そのエントリーをスキップする（発注自体を行わず、`on_order_placed`も呼ばれない）
- 価格が異なる、方向が異なる、またはクールダウン経過後であれば通常通りエントリーする
- スキップ時は`[SKIP] entry blocked by cooldown: side=... price=... elapsed=...`をログ出力する（絵文字なし）

**設計上の注意**:
- `ENTRY_COOLDOWN_AFTER_CANCEL_SEC`は**`config.json`のパラメータにはしていない**。AI夜間レビュー（5.4.2）の調整対象に含めると、`imbalance_entry_threshold`との相互作用（AIがエントリー閾値だけを動かし、クールダウン時間を考慮しない可能性）で意図しない挙動を招くリスクがあるため、engine内部の固定値として扱う方針とした
- サーキットブレーカー本体（`check_order_rate_limit()` / `record_order_event()`、3.4.1）には一切手を入れていない。クールダウンは「ノイズによる不要な発注そのものを減らす」対処であり、サーキットブレーカーの検知能力（本物の暴走を止める機能）はそのまま維持される
- メンテナンス強制キャンセル（`_force_cancel_maintenance()`）はクールダウン記録の対象外とした。メンテナンス時間帯は`_is_entry_blocked()`により新規エントリー自体が既にブロックされているため（3.2参照）、対象に含めても意味を持たない
- **2秒→3秒への変更（2026-07-15）**: 2026-07-15の0時台、`early_morning`プロファイル下でサーキットブレーカーが2回誘発された。ログを確認したところ、同一価格でのキャンセル→再エントリーの間隔がちょうど2〜3秒に収まり2秒クールダウンをすり抜けているケースが確認できたため、3秒へ延長した。効果は経過観察中。なお、同時に確認された「価格が1ティック動いただけで同一価格判定から外れ、クールダウンが効かないケース」については、秒数の変更では解決しない既知の残課題であり、今回は対応を見送った（頻度が改善しない場合は、同一方向のみでの判定への変更を再検討する）→ **2026-07-30、imbalance反転によるキャンセルに限り同一方向判定へ変更（3.4.35）**

**テスト**: `test_virtual_trader.py`に6件追加。クールダウン中・同一価格（スキップ）／経過後・同一価格（エントリー可）／クールダウン中・価格違い（エントリー可）／クールダウン中・方向違い（エントリー可）／ちょうど2.0秒経過の境界値（エントリー可、`経過 < 2秒`でのみスキップする実装のため）／`_cancel_order`による状態記録の確認。

#### 3.4.8 日次損失サーキットブレーカー（`virtual_trader.py` / `trading_engine.py`、2026-07-14実装）

**背景**: 9章で長らく未実装とされていた「1日の累積損益が開始時残高の一定割合を超えて悪化したら停止する」安全装置。3.4.1（発注レート）・3.4.2（実口座整合性）がシステムの異常動作を検知する仕組みであるのに対し、こちらは正常動作の結果としての累積損失そのものを見る、性質の異なる安全装置として独立に実装した。

**判定対象と挙動**: 判定対象は**実現損益のみ**（未実現の含み損は対象外）。閾値超過時は3.4.1・3.4.2と同じ`manual_stop.flag`経路（既存の`_trigger_safety_stop`）による**緊急停止**（保有ポジション強制決済込み）とする方針で実装した（新規エントリーのみ止める案も検討したが、3.4節の「実資金投入前の最終防衛ライン」という位置づけに合わせ、他の安全装置と挙動を揃えた）。

**config.jsonへの追加項目**
```json
{
  "daily_loss_limit_pct": 0.10
}
```
`config_manager.py`の`apply_engine_safety_defaults()` / `default_config_payload()`で、値が存在しない場合はデフォルト値（0.10）を自動補完する。

**「1日」の境界とエンジン再起動をまたいだ状態の永続化**: 1.2節の通り、このシステムの「1日」は暦日00:00ではなく06:00のエンジン再起動が起点であるため、単純に「エンジン起動時点の残高」を基準にすると、クラッシュ再起動等（06:00以外のタイミングでの再起動）のたびに基準がリセットされ、実質的に閾値をすり抜けてしまう。この対策として、`live_state.db`に以下を永続化し、起動時に「新しい06:00サイクルか、同じサイクルの再開か」を判定する設計とした。

- `trading_day_date`（直近06:00時点の日付）
- `daily_start_balance`（そのサイクル開始時点の内部シミュレーション残高）
- `daily_realized_pnl`（そのサイクル内の実現損益累計）

`virtual_trader.py`側の実装：
- `calc_trading_day_date()`: 現在時刻から「直近06:00サイクルの日付」を算出する
- `initialize_daily_loss_state()`: 起動時、`live_state.db`から読み込んだ`trading_day_date`と`calc_trading_day_date()`の結果を比較し、同一なら`daily_start_balance`/`daily_realized_pnl`を引き継ぎ、異なれば新サイクルとして`daily_start_balance`を現在残高で初期化・`daily_realized_pnl`を0にリセットする
- `check_daily_loss_limit()`: `daily_realized_pnl <= -(daily_start_balance * daily_loss_limit_pct)`を満たしたら`on_daily_loss_limit`コールバック経由で既存の`_trigger_safety_stop`（3.4.3の`manual_stop.flag`生成経路）を呼ぶ

`trading_engine.py`側の実装：
- `live_state`テーブルに`trading_day_date`/`daily_start_balance`/`daily_realized_pnl`カラムを追加・マイグレーション
- 起動時にDBから読み込み、`initialize_daily_loss_state()`を呼ぶ
- 1秒ごとの`_write_live_state()`でこれら3カラムも永続化する

**実現損益の更新タイミング**: `daily_realized_pnl`は以下いずれの決済でも更新され、直後に`check_daily_loss_limit()`が呼ばれる（2026-07-14、当初はTAKE_PROFIT/STOP_LOSSのみだったが、9章の元の要件「その日の実現損益」がTP/SLに限定されていないため、FORCE_CLOSE_MAINTENANCEも対象に追加した）。
- `TAKE_PROFIT` / `STOP_LOSS`
- `FORCE_CLOSE_MAINTENANCE`（メンテナンス強制決済、3.2節）

**設計上の注意**:
- `check_order_rate_limit()`・`compare_with_internal_state()`等、3.4.1・3.4.2の既存ロジックには一切手を入れていない
- **運転中に06:00をまたいでも、エンジンが再起動しない限り日次リセットは走らない**（要件は起動時比較のため）。ネイティブ環境では`BTC_Engine_Restart`タスクが06:00に必ず再起動をかけるため実運用上は問題ない。VPS/Docker本番環境についても、2026-07-14に`engine-restart.timer`（4.5.7）を追加済みのため、同様に06:00の再起動が保証される、という想定だった。**2026-08-20、`manual_stop.flag`存在時の`engine-restart`SKIP（4.5.7）と重なり、この想定が崩れて約15時間ロールオーバーが遅延する事象が実際に発生した。2026-08-21、ループ内で実時刻ベースにロールオーバーする恒久対応を追加し解消した（3.4.53、7.27）**
- `scripts/reset_trading_state.py`は新カラムに未対応だったため、手動リセット直後は`trading_day_date`等がNULLになり得る問題があったが、2026-07-15に解消した（エンジン起動時の`initialize_daily_loss_state()`で新サイクル扱いとして初期化されるため実害は小さかったが、`CREATE TABLE`文自体にも`engine_status`を含む複数カラムが漏れていたため、`trading_engine.py`の`_CREATE_TABLE_SQL`と全カラムを突き合わせて解消した）

**テスト**: `test_virtual_trader.py`に追加。閾値未満／ちょうど閾値／閾値超過の境界値、未実現損益のみでは発火しないことの確認、同一`trading_day_date`での引き継ぎ、06:00をまたいだ場合のリセット、STOP_LOSS経由での累積＋発火、FORCE_CLOSE_MAINTENANCEの反映、FORCE_CLOSE_MAINTENANCE単独での発火、STOP_LOSSとの合算での発火。

#### 3.4.9 CSVとlive_state.dbの整合性チェック（`scripts/check_csv_db_consistency.py`、2026-07-14実装）

**背景**: 9章で長らく未実装とされていた「その日のCSVの決済件数とlive_state.dbのカウンター増分を突き合わせ、乖離していたら警告する」仕組み。7.1（二重起動によるデータ汚染）・7.3（動的サイジングの複利暴走）はいずれも、この仕組みがあれば早期発見できたと考えられる。既存の`check_trading_anomaly.py`（1時間毎の取引頻度・損益急変監視）とは観点が異なる独立の仕組みとして、新規スクリプトで実装した（`check_trading_anomaly.py`・`virtual_trader.py`のロジックには一切手を入れていない）。

**比較対象**: 1時間ごとに以下2つの値の**増分**を比較する。
- `live_state.db`の`win_count + loss_count`
- 当日の`log/realtime_trading_log_YYYY-MM-DD.csv`で`reason`が`TAKE_PROFIT` / `STOP_LOSS` / `FORCE_CLOSE_MAINTENANCE`である行数（該当CSVが存在しない場合は0件）

**「日」の境界はCSV側（暦日00:00）に合わせている**点に注意。3.4.8の`daily_realized_pnl`は06:00起点のため、両者は別の「日」概念であり、コード上も意図的に独立させている。

**エンジン再起動によるカウンターリセットへの対応（7.11と同じ考え方）**: `live_state.db`の`win_count`/`loss_count`はエンジン再起動のたびに（サーキットブレーカー発動時・06:00定例再起動いずれも）リセットされる（7.11参照）。このためDB側カウンターが前回チェック時より減っていた場合、または日付が前回チェックから変わっていた場合（CSVローテーション）は、閾値判定をスキップし、現在値をそのまま新しいベースラインとして保存するのみとする。`check_trading_anomaly.py`が7.11で追加した対応と同じ考え方を踏襲している。

**再起動の裏付け確認（PIDチェック、2026-07-15追加）**: 上記の「DBカウンター減少＝再起動」という判定は、06:00の定例再起動が毎日発生する以上、頻繁に成立する。もしCSVとDBの乖離が実際の再起動タイミングと重なって発生していた場合、この判定だけでは検知できず、長期間見逃されるリスクがある（リアルタイム化による頻度向上では解決しない構造的な穴）。このため、DBカウンター減少を検知した際は、`runtime/trading_engine.pid`の中身（PID）が前回チェック時と実際に変化しているかで裏付けを取るようにした。

- 状態ファイル（`runtime/csv_db_consistency_state.json`）に`last_engine_pid`（PIDファイルが存在しない場合は`null`）を追加で保存する
- DBカウンター減少時、PIDが前回と異なっていれば（`null`⇔値の変化を含む）正当な再起動とみなし、従来通り比較をスキップして通知しない
- PIDが前回と同一であれば、「再起動の形跡なくカウンターが減少」という、それ自体が異常な状態とみなし、`scripts/telegram_notifier.py`経由でTelegram警告を送る（前回値・今回値・PIDを本文に含む）
- いずれの場合も、比較後は今回の値（DBカウンター・CSV件数・PID・日付）を新しいベースラインとして保存する

なお、リアルタイム（秒単位・分単位）化による検知そのものは採用しなかった。増分比較方式（前回チェックからの差分を見る設計）により、単一のチェック間隔内で発生した乖離は次回チェック（最大1時間後）で検知できるため、頻度を上げることの効果は限定的と判断した。それよりも、上記のリセット判定の抜け穴（頻繁に発生する再起動のたびに無条件でスキップされる問題）を塞ぐ方を優先した。

**状態の永続化**: `runtime/csv_db_consistency_state.json`に、前回チェック時点の日付・DB側カウンター値・CSV側件数（`check_date` / `db_exit_count` / `csv_exit_count`）を保存する。多重実行防止のロックファイルは`runtime/check_csv_db_consistency.lock`（`run_anomaly_check.ps1`と同じ方式）。

**通知**: DB側増分とCSV側増分が一致しない場合のみ、`scripts/telegram_notifier.py`の`send_telegram_message()`で警告を送る（増分不一致の場合のみ送信、一致していれば送信しない。通知本文にDB側増分・CSV側増分・差分を含む）。実行スクリプトは`scripts/run_csv_consistency_check.ps1`（`run_anomaly_check.ps1`と同じロック・ログ・try/catch/finally構成）。ログ出力は`log/csv_db_consistency_YYYY-MM-DD.log`。

**タスクスケジューラ登録（2026-07-14実施）**: `BTC_CSV_Consistency_Check`として毎時登録済み（4.3参照、IgnoreNew設定）。登録後の初回実機実行で、`db_exit_count=23`・`csv_exit_count=110`（06:00の定例再起動でDB側カウンターがリセットされた直後だったため、CSV側の当日累積件数と絶対値としては一致しないのが正常）を確認し、初回実行として比較スキップ・ベースライン作成が正しく行われることを確認した。

**テスト**: `tests/test_csv_db_consistency.py`（7件、すべてパス）。DB増分とCSV増分が一致する場合に通知が送られないこと、一致しない場合に通知が送られること（送信部分はモック化）、DB側カウンター減少時（再起動リセット）はベースライン更新のみで通知されないこと、日付変更時も同様、対象日のCSVファイルが存在しない場合にエラーにならず0件として扱われること、DBカウンター減少＋PID変化時は比較スキップ・通知なし（`null`⇔値の変化を含む）、DBカウンター減少＋PID同一時はTelegram警告が送られること。

#### 3.4.10 実資金移行フラグ`trading_mode`（`config_manager.py` / `trading_engine.py`、2026-07-15実装）

**背景**: 現状のローカル運用は実資金投入前の仮想モード（内部シミュレーションのみ、APIキーは参照専用）。今後、実資金投入（本番化）へ段階的に移行するにあたり、稼働中のシステムを長時間止めずに準備を進められるよう、発注ロジックの実装（別タスク）に先立って、モード切り替えの土台となるフラグをまず導入した。

**実装内容**:
- `config.json`のトップレベル（`profiles`配下ではなく、`order_rate_limit_per_minute`等と同じ階層）に`trading_mode`（`"virtual"` / `"real"`）を追加した。現在の値は`"virtual"`
- `apply_engine_safety_defaults()`で、キーが存在しない場合は**必ず`"virtual"`側に**補完する（安全側デフォルト）
- `"virtual"` / `"real"`以外の値だった場合は`ConfigValidationError`とし、`trading_engine.main()`冒頭（PIDファイル・live_state.db更新より前）で`SystemExit(1)`にする。既存のプロファイル定義不正時と同じ「壊れた設定で起動しない」設計に揃えている
- `trading_engine.py`の起動ログに`config_version`の直後で`trading_mode=...`を出力し、現在どちらのモードで動いているかログから常に判別できるようにした

**AI夜間レビューからの保護（確認済み、2026-07-15）**: `safe_update_config()`は`version`・`updated_reason`・`profiles`の3キーのみを明示的に書き換える実装であり、`trading_mode`を含むそれ以外のトップレベルキーは`dict(current_config)`からの浅いコピーでそのまま保持される。`pending_rollouts`の書き込み対象も`profiles.<profile_name>.<param_key>`形式のパスに限定されており、`trading_mode`は対象にならない。Moderatorが仮に`trading_mode`を含むJSONを返しても、現状のロジックでは適用されない。**将来、Moderator出力を丸ごとマージする実装に変更する場合は、トップレベルキーのallowlistを検討する必要がある**（未実装・提案のみ）。

**テスト**: `test_config_manager.py`に4件追加（`trading_mode`欠落時の`"virtual"`補完、不正値での異常終了・状態ファイル不変更、`"virtual"`/`"real"`それぞれの正常読み込み）、すべてパス。

**今後**: 発注ロジックの抽象化（`trading_mode="real"`時にGMO Private APIへ実発注する経路の実装）は別タスクとして進める。現時点では`trading_mode`を`"real"`にしても対応する実行ロジックは存在しない。

#### 3.4.11 Private WebSocket接続基盤`PrivateWebSocketManager`（`websocket_manager.py` / `trading_engine.py`、2026-07-15実装）

**背景**: real modeでのTP/SLは、価格監視による自前シミュレーションではなく、実際の指値（TP）・逆指値（SL）注文をGMOに発注して待つ方式とすることを決定（3.4.12予定）。GMOコインの暗号資産APIはIFD/OCOをネイティブサポートしないため、TP約定検知後にSL注文をキャンセルする「合成OCO」をアプリ側で実装する必要があり、その約定検知にはPrivate WebSocket（`executionEvents`・`orderEvents`）を使う。本節はその接続基盤のみで、`VirtualTrader`との連携（合成OCOの実装含む）は別タスク。

**設計方針（案A・ミラー構成）**: 既存の`WebSocketManager`（Public、板情報）は変更せず、同型の専用daemonスレッド・`_run_loop`・指数バックオフ（基準3秒、`min(3*2^(attempt-1),60)`秒）を持つ別クラス`PrivateWebSocketManager`を新設した。認証・トークン管理・購読チャネル・ペイロード処理はPrivate専用のため共通基底クラスへの統合は行わず、責務を分離している。

**実装内容**:
- 接続: `POST /private/v1/ws-auth`でトークン取得（有効期限60分）→`wss://api.coin.z.com/ws/private/v1/<token>`へ接続→`executionEvents`・`orderEvents`をsubscribe
- トークン延長: 45分ごとに`PUT /private/v1/ws-auth`（内部タイマースレッド）。失敗時はトークンを破棄しWebSocketを閉じて再接続ループへ
- 失効検知: `on_error`/`on_close`の内容が認証系（401/403/unauthorized/invalid token/auth等の文言）と判断された場合、トークンを無効化し次回再接続時に`POST`で再取得
- 停止: `stop()`で`DELETE /private/v1/ws-auth`によりトークンを明示的に削除
- コールバック受け口: `on_execution_callback`・`on_order_callback`（Public側の`on_snapshot_callback`と同じ、コンストラクタ引数での登録方式）。現時点では受信payloadをログ出力するのみで、`VirtualTrader`へは未接続
- `trading_engine.py`: `trading_mode=="real"`の場合のみ`PrivateWebSocketManager`を生成・起動。`trading_mode=="virtual"`では一切起動しない（既存の仮想稼働経路への影響ゼロ）

**タイマースレッドのライフサイクル不整合の修正（同日中に発覚・修正）**: 実装直後の確認で、`stop()`実行時に延長HTTP（最大10秒程度）が実行中だと`join(timeout=1.0)`がタイムアウトしてしまい、延長用タイマースレッドが停止しきらないまま残留し、無効化されたトークンへ`PUT`を送ってしまう可能性があることが判明した。以下の修正を行った。
- `_renew_generation`（世代ID）を導入し、停止・再接続のたびに無効化することで、古い世代のタイマーがそれ以降なにもしないようにした
- タイマーを起動時点のトークンに紐付け、現在のトークンと不一致なら延長処理を行わないようにした
- `join`の待機を最大12秒に延長し、延長HTTPのタイムアウト（10秒）を確実にカバーするようにした
- `_run_loop`・`_connect_once`の`finally`句でも、タイマーの停止を必ず行うようにした

**テスト**: `test_private_websocket_manager.py`（3件、すべてパス）。`stop()`で延長タイマーが確実にjoinされること、多重再接続してもタイマースレッド数が増え続けないこと、トークン差し替え後に旧トークンへの`PUT`が送られないこと。

**今後**: `VirtualTrader`との連携（ENTRY約定検知、TP/SL用のLIMIT/STOP同時発注、合成OCOのキャンセル処理、`PositionState`への`tp_order_id`/`sl_order_id`追加）は別タスクとして進める。

#### 3.4.12 `manual_stop.flag`のreal mode版強制決済`_force_close_real()`（`virtual_trader.py`、2026-07-15実装）

**背景**: real modeでの緊急停止は、仮想モードの`_force_close_maintenance()`のようにローカル状態をリセットするだけでは不十分で、GMOへ実際にキャンセル・決済のAPIリクエストを送り、それが本当に成立したことを確認する必要がある。事前に3つの論点（再起動時の状態復元、緊急停止のreal mode版、発注APIエラー時の扱い）を整理した上で実装した。

**前提**: `PositionState`に`tp_order_id`・`sl_order_id`（`Optional[int]`、デフォルト`None`）を追加。real modeでのENTRY/TP/SL発注ロジック自体（3.4.13予定）は未実装のため、現時点では常に`None`。

**GMO Private APIラッパー追加**: 既存の認証処理を`_gmo_private_request`として一般化し、以下を追加。
- `gmo_cancel_order`（`POST /v1/cancelOrder`）
- `gmo_close_order`（`POST /v1/closeOrder`、`executionType=MARKET` + `settlePosition`）
- `fetch_open_positions`（既存の3.4.2の建玉取得経路を共通化して再利用）

**`_force_close_real()`の処理順序**:
1. `tp_order_id`・`sl_order_id`が設定されていれば、それぞれ`gmo_cancel_order`を呼ぶ。`ERR-5122`（変更中・取消中・取消済・全量約定・失効のいずれかで変更・取消不可）・`ERR-5123`（指定注文番号が存在しない）は、キャンセル不要とみなし正常系として処理を継続する（GMOの公式エラーコード定義に基づく。ただし`ERR-5123`は「約定済み」に限らずID誤り等も含みうるため、緊急停止時は「取消できないが続行してよい」扱いとして両方をbenignにしている）
2. `fetch_open_positions()`で建玉の有無を再確認（1の最中にTP/SLが約定して建玉が無くなっている可能性があるため）
3. 建玉が残っていれば、建玉のside（BUY/SELL）を反転させたsideで`gmo_close_order`（MARKET）を呼び成行決済
4. **決済成立の確認**: `closeOrder`成功後、`fetch_open_positions()`で確認。まだ残っていれば1秒待って再確認（最大2回まで、合計3回の確認機会）。3回とも残っている場合に限り、このattemptを失敗として扱う。Private WSは接続基盤のみで約定紐付けが未実装のため、WSでの確認ではなくRESTでの確認とした（3.4.13で実装予定の連携後に見直しうる）
5. 3のclose注文API自体が失敗、または4で3回とも決済未確認だった場合、指数バックオフ（1秒・2秒）で最大3回リトライする。3回失敗しても解消しない場合は、`[CRITICAL]`として通常より強い扱いでTelegram通知する（60秒クールダウンを設け、再試行・再通知の連打を抑制）

**呼び出し経路**: `trading_engine.py`が`trading_mode`と`on_critical_alert`（Telegram通知関数）を`VirtualTrader`に渡し、`manual_stop.flag`検知箇所（`_update_maintenance_state`）内で`trading_mode`に応じて`_force_close_real()`（real）または既存の`_force_close_maintenance()`系処理（virtual）に分岐する。起動時にすでに`manual_stop.flag`が存在するケースも同じ経路（初回の板更新時に実行）。

**永続的な再試行**: 3回のリトライすべて失敗しCRITICAL通知を送った後も、`manual_stop.flag`・内部ポジションともにクリアしないため、次回以降の板更新のたびに`_update_maintenance_state`→`_force_close_real()`が再度呼ばれる。ただし直後の毎tickではなく、失敗のたびに`_force_close_real_cooldown_until`を60秒先に設定し、その間は処理の先頭でreturnする。つまり「1tickで3回失敗して終わり」ではなく「60秒クールダウンを挟みつつ、`manual_stop.flag`がある限り永続的に再試行する」設計。

**テスト**: `test_force_close_real.py`（7件、すべてパス）。決済成立確認の遅延反映で2回目の確認で成功するケース、3回とも残存し失敗扱いになるケース、既存の即時消滅ケースを含む。

**今後（本番投入前に別途必要）**:
- `ERR-5122`/`ERR-5123`が実際のエラーレスポンスで想定どおりの`message_code`として返ってくるかの実機検証（`_force_cancel_maintenance()`側は2026-07-29に実機確認済み、詳細は3.4.21。本項の`_force_close_real()`側TP/SLキャンセル経路は未検証）
- `VirtualTrader`との連携（ENTRY約定検知、TP/SL同時発注、合成OCO本体）
- 再起動時の状態復元（GMOの建玉・有効注文からの再構築。建玉あり・TP/SL不一致時は自動復旧を試み、同一建玉で2回目が発生した場合は自動復旧せず安全停止に切り替える設計）
- 3.4.2の拡張（孤児注文チェック）

#### 3.4.13 エンジン再起動時の内部残高・KPIの復元（`virtual_trader.py` / `trading_engine.py`、2026-07-19実装）

**背景**: AIコードレビューにより、`VirtualTrader`の`jpy_balance`（内部円残高）が、エンジン起動のたびに常に固定値（`initial_jpy`引数、5万円）で初期化されており、直前セッションの残高を一切引き継いでいないことが判明した（実害発生前に発見）。`win_count` / `loss_count` / `total_gross_win` / `total_gross_loss` / `cumulative_pnl`も同様に、起動のたびにゼロへリセットされていた。3.4.8で`daily_start_balance` / `daily_realized_pnl`のみは`live_state.db`から復元する実装が既にあったが、`jpy_balance`本体はその対象に含まれていなかった。

**real modeでの想定される実害**: 3.4.2の`get_internal_account_state()`は`trader.jpy_balance`をそのままreconciliation比較に使う。real mode運用中にエンジンが再起動（クラッシュ・デプロイ・06:00定例再起動いずれも）すると、`jpy_balance`が実際の口座残高ではなく固定値5万円にリセットされ、次回の`run_reconciliation_check()`で残高差`reconciliation_tolerance_jpy`（デフォルト100円）を確実に超過する。2回連続不一致で3.4.2の`manual_stop.flag`（`reconciliation_mismatch`）が誤発動し、real mode移行後は再起動のたびにほぼ確実に緊急停止する構造になっていた。

**実装内容**:
- `live_state`テーブルには`jpy_balance` / `win_count` / `loss_count` / `total_gross_win` / `total_gross_loss` / `cumulative_pnl`のカラムが3.4.8以前から既に存在していたため、スキーマ変更は不要だった
- `trading_engine.py`の`_load_daily_loss_persisted()`のSELECT文に上記6カラムを追加し、戻り値の辞書に含めるようにした
- `virtual_trader.py`の`VirtualTrader`に新規メソッド`restore_persisted_account_state()`を追加。引数（上記6項目）が`None`でない場合のみ対応する内部属性を上書きする（`win_count=0`や`cumulative_pnl=0.0`のような正当な値を`None`と誤判定しないよう、判定は全て`is not None`で行う）。値が`None`の場合（`live_state.db`未作成の初回起動時等）はコンストラクタのデフォルト値を維持する
- `_initial_jpy`（コンストラクタ引数のスナップショット、`realized_pnl`プロパティの算出基準）には触れず、`jpy_balance`本体のみを上書きする
- `trading_engine.py`の`main()`で、`VirtualTrader`インスタンス化直後・`initialize_daily_loss_state()`呼び出しより**前**に`restore_persisted_account_state()`を呼ぶ。3.4.8の`initialize_daily_loss_state()`は新しい取引日サイクル開始時に`daily_start_balance`を`self.jpy_balance`から算出するため、呼び出し順序を誤ると、日付が変わった直後の再起動時に誤った（リセットされた）残高が新サイクルの基準値として使われてしまう

**テスト**: 既存ユニットテスト56件すべてパス。

---

#### 3.4.14 エンジン再起動時のポジション（建玉）未復元による資金消失の修正（`virtual_trader.py` / `trading_engine.py`、2026-07-23実装）

**背景**: 3.4.13の対応は`jpy_balance`・`win_count`・`cumulative_pnl`等の**カウンタ**の復元のみを対象としており、**オープン中のポジション自体**（サイド・数量・エントリー価格等）は復元対象に含まれていなかった。LONG/SHORTエントリー時は購入代金・拘束分が`jpy_balance`から差し引かれる設計のため、ポジションを保持したままエンジンが再起動すると、「現金は減ったまま・ポジションだけFLATにリセットされる」状態になり、拘束されていた資金が帳簿から消失する。`live_state.db`には`position_side`等の列自体は3.4.13以前から存在し書き込まれていたが、起動時の`_load_daily_loss_persisted()` / `restore_persisted_account_state()`がこれらを読み出していなかったことが原因。

**発覚の経緯**: 公開レポート機能の実装（4.6）に伴う調査の過程で「テスト後に総資産が減った」との申告があり（7.24、こちらは表示ラベルの混在であり本件とは別事象と判明）、その後改めて「累積損益はプラスなのに総資産が2万円以上少ない」という実測値の食い違いから本件が発覚した。実測時点（2026-07-23）で、初期資産5万円＋累積損益(+73円)に対し、実際の現金残高は約28,490円まで低下しており、約1.07万円の欠損が生じていた（7/21前後・7/22夜の2回のLONG保有中再起動が原因）。

**real modeでの想定される実害**: `VirtualTrader`はvirtual mode専用ではなく、real mode移行後も内部の状態管理（現金・ポジション・損益）の中核として使われる設計である。real mode下で本バグが発生すると、GMOコイン上に実際のポジションが残っているにもかかわらず、システム内部では「ポジションなし」と誤認識し、以降の安全装置（サーキットブレーカー等）がそのポジションを一切考慮しなくなる。3.4.13のreconciliation誤発動よりもさらに重大で、実資金がマーケットリスクに晒されたまま監視対象から外れる。

**実装内容**:
- 起動時に`live_state.db`からオープン中のポジション情報（`position_side` / `position_entry_price` / `position_size` / `position_is_pending` / `position_exit_target` / 約定時刻）を読み出し、`VirtualTrader.position`へ復元する処理を追加（約定時刻は今回新たに永続化・復元対象に追加）
- ポジション情報が復元できない場合のフォールバック: 可能な範囲でLONG拘束額を現金へ戻し、Telegramで即時アラート（黙って資金が消えることを防ぐ）
- 起動時整合性チェック: 期待現金（初期資産＋累積損益－現在の拘束額）と実際の現金残高、および前回記録された総資産との乖離を監視し、しきい値（3,000円）を超えた場合にアラート
- 既存の欠損補正用に`scripts/repair_live_state_equity.py`を新規作成（デフォルトはdry-run、`--apply`で適用）
- 2026-07-23、エンジン停止 → 補正スクリプト適用（現金残高 28,494.51円 → 50,086.04円）→ エンジン再起動を実施。再起動時に保有中のSHORTポジションが正しく復元されることをダッシュボードで確認済み（本番相当の状況での初回実地検証も兼ねた）

**今回の対応範囲外（別タスクとして検討）**:
- 日次メンテナンス前の強制決済（土曜と同じ仕様）の追加（本件の直接の再発防止策にはなるが、意図的に見送り。オーバーナイト保有を許容する現在の戦略設計を変更することになるため、別途検討する）
- real mode起動時のGMO取引所側の建玉との突合の必須化（3.4.12の「今後」参照）

**公開レポートへの影響**: Xの公開レポート（4.6）のうち、累積損益(円)は決済済みトレードのみを積み上げる値のため本バグの影響を受けない。一方、`total_assets_eod`（現金＋ポジション評価）を用いる当日増減率(%)は、本バグが発生していた期間は不正確な値になっていた可能性がある。本修正および欠損補正により、以降は正しい値になる。

**テスト**: LONG/SHORT保有中の再起動シナリオでの復元、復元不能時のフォールバック、整合性チェックを検証するテストを追加。207件すべてパス。

---

#### 3.4.15 ネイティブ運用でのエンジン完全停止の検知・自動復旧（`scripts/check_engine_process.py`、2026-07-23実装）

**背景**: 4.7の内部日次レポート拡張（監視系ハートビート状況の可視化）をきっかけに、`check_engine_crash_loop.py`の心拍が3日近く更新されていない（STALE）ことが発覚。調査の結果、2つの問題が判明した。

1. `BTC_Crash_Loop_Check`タスクが7/20以降、ログオンなし状態で定期実行が止まっていた（後日確認したところ既にLogonType=Passwordで解決済みだったことが判明し、追加対応は不要だった）
2. より本質的な問題として、`check_engine_crash_loop.py`はDocker Compose（`engine` / `dashboard`サービス）の状態監視を前提にした設計であり、現在のネイティブWindows Python運用ではチェック対象のサービス自体が存在しないため、**心拍は正常に更新されるが実質何も監視していない**状態だった。既存の`BTC_Detect_Orphan_Engines`（`detect_orphan_engines.ps1`）も、多重起動・pidファイル不整合の検知が目的であり、「エンジンが完全に停止した」ことの検知・通知は行っていない（ログ追記のみ、Telegram通知なし）

**実装内容**:
- 新規`scripts/check_engine_process.py`: エンジンプロセスが0件、かつ`runtime/manual_stop.flag`が存在しない場合（＝意図しない完全停止）を検知する。誤検知防止のため90秒の猶予期間を設けてから確定判定する
- 検知した場合、`ensure_engine_running.ps1`を呼び出して自動復旧を試み、「検知→復旧試行→結果」を1通のTelegramアラート（内部Bot）にまとめて通知する
- `check_monitor_heartbeats.py` / `build_daily_report.py`の心拍SLA監視対象に`check_engine_process`を追加
- `BTC_Engine_Process_Check`としてTask Schedulerに登録（5分間隔。当初1時間間隔の案があったが、完全停止の検知が遅れすぎるため5分に短縮した）
- 既存の全`BTC_*`タスクのログオン設定を一覧確認し、「対話型のみ」のまま残っているものが無いことを確認済み（2026-07-23時点）

**役割の整理**（今後のためのメモ）:
- `check_engine_crash_loop.py`（Docker Compose向け）: VPS/Linux移行後、Docker運用に戻った際に有効
- `BTC_Detect_Orphan_Engines`（`detect_orphan_engines.ps1`）: 多重起動・pidファイル不整合の検知（ログのみ、通知なし）
- `check_engine_process.py`（今回追加）: ネイティブ運用でのエンジン完全停止の検知・自動復旧（Telegram通知あり）
- 将来的にこれら3つの役割が重複・混同しやすいため、VPS移行時にドキュメントとして改めて整理する（9章）

**テスト**: エンジンプロセス0件・`manual_stop.flag`有無による挙動の違い、自動復旧処理の呼び出しを検証するテストを追加。222件すべてパス。

---

#### 3.4.16 フェーズ2：認証・シンボルまわりの起動不能解消（2026-07-26実装）

**背景**: 隔離テスト環境（`docker-compose.real-test.yml`、4.5.9）を用いてGMOの実口座に初めてコードを通したところ、フェーズ1（発注ロジック本体）だけでは想定していなかった起動不能の問題が複数発覚した。

| 問題 | 原因 | 対応 |
|---|---|---|
| `ERR-5010`（署名無効） | GETリクエストの署名文字列にクエリパラメータ（`?symbol=...`）を含めてしまっていた | 署名対象からクエリを除外し、実際のリクエストURLにはクエリ付きパスを使うよう分離 |
| `ERR-5106`（symbol不正） | `_GMO_LEVERAGE_SYMBOL = "BTC"`が誤り。GMO公式パラメータ一覧で**`BTC`=現物、`BTC_JPY`=レバレッジ**と確定 | 発注・決済・建玉参照系すべてを`BTC_JPY`に統一 |
| `ERR-5012`（権限不足） | APIキーに「余力情報を取得」権限が未付与 | 権限追加 |
| `ERR-201`（証拠金不足） | レバレッジ口座に証拠金を入れていなかった | 現物からレバレッジ区分へ資金振替 |

- GMOの`/v1/symbols`で`BTC_JPY`の最小注文数量・呼値を確認した結果、最小発注単位は`0.001 BTC`であることが確定した（それまで仕様書・コード中にあった`0.01`という記載は誤りであり訂正）。`MIN_TRADE_SIZE`/`LOT_UNIT`を`0.001`に修正
- APIキーを発注系`GMO_API_KEY_TRADE`/`GMO_API_SECRET_TRADE`と参照専用`GMO_API_KEY_READONLY`/`GMO_API_SECRET_READONLY`（孤児注文チェック等が使用）に分離した（3.4.5で述べた分離方針の実装完了）。`websocket_manager.py`のPrivate WS認証情報取得も新変数名に統一

#### 3.4.17 決済方式の抜本的な設計変更（2026-07-26〜27実装、最重要）

**背景**: GMO公式サポートへの確認により、レバレッジ取引の決済には「反対売買」ではなく**決済注文**（`positionId`指定、`/v1/closeOrder`）が必要であり、通常の新規注文で反対売買を行うと決済されずに別の新しい建玉ができてしまう（買い・売り両建てが発生する）ことが実弾検証で発覚した。3.4.12時点の設計・実装はこの前提を踏まえていなかったため、見直しが必要になった。

- TP・SLを両方`closeOrder`でフルサイズ同時発注すると`ERR-200`（決済可能数量超過）になることを検証で確認した。最小発注単位`0.001 BTC`は分割不可のため、TP/SLの両方を同時にGMO側へ常設しておくこと自体が不可能と判明した
- **新設計**: SLのみ`gmo_close_order`（STOP、フルサイズ、`positionId`指定）でGMO側に常設する。TPは常設せず、板監視でTP水準到達を検知したら「SLキャンセル→`gmo_close_order`（MARKET）で成行決済」という順で処理する
- `PositionState`に`position_id`（GMO建玉ID）を追加し、通常約定時・キャンセル不可時（＝既に約定済みとして建玉を採用する経路）・起動時突き合わせの各経路で保持するよう整備した
- 起動時突き合わせ（`reconcile_real_state_on_startup()`、3.4.12参照）を新設計に追従させた：`tp_order_id is None`は正常状態として扱い、SLの欠落のみを判定・再発注するよう修正した（旧ロジックのままだと、ポジション保有中に再起動するたびTP未発注を異常とみなし誤って安全停止していた）
- `gmo_close_order`に`price`/`time_in_force`を渡せるよう拡張した（従来はMARKETのみ対応）

#### 3.4.18 内部残高会計の非対称修正（LONG/SHORT、2026-07-26〜27実装）

**背景**: SHORTは元々「証拠金取引前提でリベートのみ反映」という設計だったが、LONGは現物的な発想（購入代金満額を拘束）のままreal modeに引き継がれていたため、real modeのLONGで随所に非対称・二重計上が発生していた。判明順に列挙する。

1. **エントリー時**: `_enter_long`（real）が想定元本込み（`-total`）で減算していたのを、SHORTと同じ`-fee`のみに統一
2. **キャンセル時の巻き戻し**: `_cancel_order`/`_force_cancel_maintenance`のLONG real分岐が、まだ拘束していない想定元本を「返却」してしまっていたのを`+fee`のみに修正
3. **TP/SL決済時**: `_settle_real_exit_from_execution_unlocked`のLONGが`fill_price*size-fee`（想定元本込み）を加算していたのを、SHORTと同じ`net_pnl`のみに修正
4. **メンテナンス前自動決済**: real modeなのに板ベースの仮想決済（`_force_close_maintenance`）が呼ばれ、GMO側の実ポジションが決済されないまま内部だけ「決済済み」になるバグを発見し、`_force_close_real`（3.4.12）へ正しく分岐するよう修正
5. **フォールバック返却**（`_fallback_release_locked_capital`）: real modeのLONGで想定元本を返却していたのを、estimated_lossベースのアラートのみに変更
6. **強制決済後の残高同期**（`_force_close_real`）: 決済成功後に残高を一切更新していなかったのを、`fetch_real_account_state`の`equity_jpy`（3.4.19参照）に同期する処理を追加
7. **ダッシュボード/日次レポートの総資産計算**（`portfolio_metrics.py`、7.24参照）: LONGの評価額が想定元本（`size×mid`）のままで、real modeでは二重計上になっていたのを`(mid-entry)×size`（含み損益のみ）に分岐
8. **口座整合性チェック**（`expected_jpy_balance`、3.4.2/3.4.14関連）: 同じく想定元本を差し引く計算のままだったのを、real modeのLONGでは差し引かないよう修正

いずれも発端は「起動時整合性チェック（`reconciliation_mismatch`）の誤検知」または「ダッシュボードの総資産表示が実態と合わない」という症状から発見した。

#### 3.4.19 認証情報・実口座残高の参照先の整理（2026-07-27実装）

- `fetch_real_account_state()`が`GET /v1/account/assets`（現物・共通資産）を見ていたのを、レバレッジの実際の余力`GET /v1/account/margin`の`availableAmount`に切り替えた（発注サイズ計算用）
- 整合性チェック用には、含み損益込みの時価評価総額`actualProfitLoss`（`equity_jpy`）を別途取得し、発注サイズ計算とは別の用途で使い分ける設計に変更した

---

#### 3.4.20〜3.4.25 Cursorレビューを受けた不具合修正（2026-07-27〜28実装）

**背景**: フェーズ2の隔離テスト環境で緊急停止(`manual_stop.flag`)を実行した際、エントリー中の指値注文がGMO側にキャンセルされずに残る実害（孤児注文、画面から手動で取り下げ）が発生した。これを受けてCursorにコードレビューを依頼し、Critical/High/Medium/Lowで分類された指摘事項を優先度順に対応した。

##### 3.4.20 保有中の口座照合の誤検知修正（Critical、`virtual_trader.py`）

`compare_with_internal_state()`が、GMOの`equity_jpy`（含み損益込みの時価評価額）と内部`jpy_balance`（含み損益を反映しない帳簿上の現金）をそのまま比較していたため、ポジション保有中は通常の値動きだけで許容値`reconciliation_tolerance_jpy`（デフォルト100円）を容易に超過し、2回連続不一致でmanual_stop.flagが誤発動しうる状態だった。

- `get_internal_account_state()`にて、ポジション保有中は`comparable_equity_jpy`（現金＋含み損益、real/virtual両対応）を算出し、GMOの`equity_jpy`と比較する方式に変更。FLAT/pending時は従来通り`jpy_balance`同士の比較を維持
- mid価格が未取得の場合は金額照合をスキップし、建玉サイズのみで照合する（`skip_balance_check`）
- 許容値(`reconciliation_tolerance_jpy`)自体は変更していない
- テスト6件追加（保有中comparable一致/不一致、旧挙動の誤検知確認、skip、FLAT回帰、統合テスト）

##### 3.4.21 緊急停止時のpendingエントリー未キャンセル修正（Critical、`virtual_trader.py`）

今回の実害（孤児注文）の直接原因。`_update_maintenance_state()`のreal mode分岐が、ポジションの状態（`is_pending`）によらず常に`_force_close_real()`（建玉決済のみ、エントリー指値のキャンセルは行わない）に処理を回していたため、エントリーが未約定のまま緊急停止がかかると、GMO側の指値注文がキャンセルされず孤児化していた。

- `is_pending=True`の場合は`_force_close_real()`ではなく`_force_cancel_maintenance()`（既存の`_cancel_real_entry_order_or_adopt_fill()`経由でGMOへ実際にキャンセルAPIを送信）に分岐するよう修正
- ERR-5122/ERR-5123（既に約定済み等）でキャンセルできない場合は、既存の建玉採用フォールバック（3.4.12関連）にそのまま合流し、SL発注まで行う
- `is_pending=False`（建玉成立済み）の場合は従来通り`_force_close_real()`を維持
- Telegram通知: キャンセル成功時／建玉採用時／キャンセル失敗（次回リトライ）時でそれぞれ内容を分ける
- テスト2件追加（cancel成功パス、ERR-5122等でのadoptフォールバックパス）

**実機検証（2026-07-29）**: 隔離環境でエントリー注文を発注後、GMO画面から手動で当該注文をキャンセルし、その状態で緊急停止を実行。ログに`real entry cancel benign and no open position; treat as cancelled orderId=8759598706 context=force_cancel_maintenance`と出力され、ダッシュボードも`[FLAT] ポジションなし（待機中）`、Telegramにも`pending entry order cancelled`として通知された。誤って「建玉採用」扱いにせず、実際に約定していないことを正しく判別してキャンセル済みとして処理しており、意図した設計通りに動作することを実機で確認できた。GMOから返った生の`message_code`（`ERR-5122`/`ERR-5123`のいずれか）自体はアプリケーションログには出力されないため未確認だが、挙動としての安全性は確認済み。この検証は`_force_cancel_maintenance()`（pendingエントリーのキャンセル経路）についてのものであり、建玉成立後・TP/SL常設済みの状態での`_force_close_real()`側のキャンセル経路（3.4.1）は未検証のまま残っている。

##### 3.4.22 発注用APIキーの多重起動排他制御（`trading_engine.py`）

`.env`の発注用キー(`GMO_API_KEY_TRADE`)は、real-test隔離環境（`docker-compose.real-test.yml`）と将来の本番環境（`docker-compose.yml`）の両方から参照される想定であり、本番切り替え時にreal-test環境の停止を忘れると同一アカウントへの二重発注が起こりうる。将来の事故を防ぐため、コードレベルの排他制御を導入した。

- `runtime/gmo_trade_key.lock`（JSON: `{"pid": <int>, "started_at": "ISO8601"}`）による排他ロックを追加。`trading_mode="real"`起動時、config読み込み直後・`VirtualTrader`初期化より前に取得を試みる
- 生存確認は~~`os.kill(pid, 0)`（`repair_live_state_equity.py`と同方式）~~ → **2026-07-31、`fcntl.flock`方式に変更（3.4.39）**。PID生存確認方式はDockerコンテナのPID名前空間再採番と相性が悪く実運用で誤検知が発生したため、詳細は3.4.39参照
- 生存プロセスが既にロックを保持していれば、Telegram通知の上`SystemExit(1)`で起動を中止する（この挙動自体は変更なし）
- ~~PIDが非生存（staleロック）の場合は削除して起動を継続する~~ → 3.4.39以降は`flock`の可否のみで判定するため、この分岐は不要になった
- 解放は`finally`ブロック＋`atexit`の二重防御
- `trading_mode="virtual"`ではロック機構を適用しない
- **既知の制約（未解決）**: 本ロックは同一ホスト・同一`runtime/`ディレクトリ内のプロセス間でのみ有効。`docker-compose.yml`（`./runtime`）と`docker-compose.real-test.yml`（`./runtime-real-test`）はホスト上のruntimeパスが別であるため、**このロックだけでは両compose間の同時起動は防げない**。本番切替時にreal-test環境の停止を確認する運用手順が別途必要（9章参照）
- テスト4件追加（正常取得／生存PIDでのブロック＋通知／stale削除／`SystemExit`確認）

##### 3.4.23 SL約定後・強制決済後のequity/KPI同期（`virtual_trader.py`）

板監視によるTP決済は`_settle_real_exit_from_execution_unlocked()`の直後に`_sync_jpy_balance_from_equity_unlocked(context="REAL-TP")`を呼んでいたが、以下2経路で同様の同期が漏れていた。

1. **SL約定（Private WS `executionEvents`経由）**: `_apply_exit_execution_unlocked()`で`is_sl`のとき、決済直後に`_sync_jpy_balance_from_equity_unlocked(context="REAL-SL")`を追加
2. **`_force_close_real()`成功後**: 建玉クリア・equity同期のみで`cumulative_pnl`・win/loss count・`trade_history`・`daily_realized_pnl`が更新されていなかった。決済成立確認（`openPositions`で残玉なしを確認）後に、板価格ベースで`_settle_real_exit_from_execution_unlocked(reason="FORCE_CLOSE_REAL")`を呼んでからequity同期する順序に変更。3回のリトライすべて失敗した場合はこの処理を通らず、KPIも更新されない（決済が成立していないため正しい）

テスト計4件追加（SL側2件は`test_real_tp_sl.py`、force close側2件は`test_force_close_real.py`）。

##### 3.4.24 `repair_live_state_equity.py`のreal mode対応（`scripts/`）

3.4.14のインシデント対応で追加された本スクリプトは、当時virtual mode前提（LONGは想定元本を差し引く会計式）のままだった。3.4.18でreal modeのLONG会計がSHORTと同じ方式（想定元本を差し引かない）に統一されたため、real modeの環境で本スクリプトを`--apply`すると、正しい残高を誤って壊しうる状態だった。

- `trading_mode`の解決順序: `live_state.db`の`trading_mode`列 → `config/config.json` → デフォルト`virtual`
- `trading_mode="real"`の場合、LONGポジションでも想定元本を差し引かない計算式に分岐（SHORT/FLATの計算式は無変更）
- real mode×LONG保有中に`--apply`する場合は`--confirm-real`フラグを必須とし、なければ`RuntimeError`で拒否
- dry-run出力にtrading_mode・読み取り元・使用した計算式を明示
- 既存の呼び出し方法（引数なしでのdry-run実行等）との後方互換性を維持
- テスト3件追加（real×LONGで想定元本を差し引かないこと、virtual×LONGは従来通り、real×LONGで`--confirm-real`なしの`--apply`が拒否されること）

##### 3.4.25 孤児注文監視の本番配備・死活監視組み込み（`scripts/check_orphan_orders.py`ほか）

`check_orphan_orders.py`（3.4.2拡張、5分間隔でTask Scheduler登録済み）は、VPS/Linux本番環境向けのsystemd定義が未整備で、かつ`check_monitor_heartbeats.py`（3.4.15）の死活監視対象にも含まれておらず、本番でこのスクリプト自体が止まっても気づけない状態だった。

- `deploy/systemd/orphan-orders-check.service`/`.timer`を新規作成（既存の`csv-consistency-check.*`と同形式、5分間隔）
- `scripts/run_orphan_orders_check.sh`を新規作成（既存の`run_csv_consistency_check.sh`等と同じ構造: ロックファイルによる多重実行防止、失敗時のTelegramアラート）
- `check_orphan_orders.py`は元々`_record_monitor_heartbeat()`を実装しており、`trading_mode≠real`でのスキップ時・孤児なし・孤児検知時のいずれの正常終了パスでも心拍を更新する（本体スクリプトの検知ロジック自体は無変更）。API例外等で異常終了した場合のみ心拍が更新されない
- `check_monitor_heartbeats.py`の`MONITOR_SLAS`に`check_orphan_orders`（20分、crash-loop-checkと同じ基準）を追加
- `build_daily_report.py`の`MONITOR_SLA_HOURS`にも同キーを追加し、日次レポートの心拍SLA可視化に反映
- **既知の制約（未解決）**: `check_orphan_orders.py`の`load_trading_mode()`は`config.json`の読み取りに失敗した場合も`virtual`扱いとしてチェックをスキップし、その場合も心拍は正常に更新される。そのため、real mode運用中にconfig.json破損等でチェック自体が実質機能していない状態でも、心拍SLA監視上は「正常」に見えてしまう（9章参照）
- テスト2件追加（`check_monitor_heartbeats.py`のSLA対象への追加確認、STALE判定確認）

#### 3.4.26 未約定エントリー指値のタイムアウトキャンセル機構（`virtual_trader.py`、2026-07-28実装）

**背景**: 実運用で指値エントリーが約2時間未約定のまま放置され、その間に市況が大きく動いたにもかかわらず、既存の`_check_pending_fill()`はimbalance条件が反転しない限り指値をそのまま据え置く設計だったため何も対応しなかった。最終的に価格が一気に動いて指値が「飲まれる」形で約定し、エントリー根拠の鮮度（2時間前の市況判断）と実際の約定タイミングの市況が乖離した状態でポジションが成立した。板の厚み・出来高・ボラティリティはAI夜間議論への集計材料止まりで、リアルタイムの約定/キャンセル判定には使われない設計（意図的な既存方針）であるため、別途この機構を新設した。

**判定条件（`_check_pending_fill()`拡張、OR条件）**:
- **経過時間タイムアウト**: 指値発注からの経過時間が`ENTRY_PENDING_TIMEOUT_MINUTES_BY_PROFILE`（プロファイル別辞書、初期値は`early_morning`/`daytime`/`night`/`full_day`いずれも60分、`ENTRY_PENDING_TIMEOUT_MINUTES_DEFAULT`をフォールバック）を超えた場合
- **価格乖離タイムアウト**: 現在のmid価格と指値価格の乖離率が、そのプロファイルの`stop_loss_pct * ENTRY_PENDING_DEVIATION_SL_RATIO`（=SL幅の50%）以上になった場合

既存のimbalance反転・スプレッド超過によるキャンセル判定を優先し、これらに該当しない場合にのみ上記2条件を評価する（`elif`）。

**キャンセル経路の統合**: 新規のキャンセル処理は作らず、既存の`_cancel_order()`をキャンセル理由付きで拡張し、`_force_cancel_maintenance()`も同じ経路を通す構成とした。real modeでは従来通り`_cancel_real_entry_order_or_adopt_fill()`を先に呼び、キャンセル不可（既に約定済み等）の場合は建玉として採用するフォールバックにそのまま合流する。

**クールダウンの出し分け**（`_cooldown_sec_for_cancel_reason()`）:
- imbalance反転によるキャンセル: 既存の`ENTRY_COOLDOWN_AFTER_CANCEL_SEC`（3秒）を維持
- 時間／価格乖離タイムアウトによるキャンセル: 新設の`ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC`（300秒＝5分）。陳腐化したシグナルのままの即時再発注ループを防ぐため、3秒クールダウンとは独立した定数として定義した
- メンテナンス強制キャンセル: 従来通りクールダウンなし（`apply_cooldown=False`）

いずれの新定数も`ENTRY_COOLDOWN_AFTER_CANCEL_SEC`（3.4.7）と同様、`config.json`には出さずモジュールレベル定数として管理し、AI夜間調整の対象外とした。

**ログ拡張（`realtime_trading_log`）**: `cancel_reason`（`IMBALANCE_REVERSAL`/`MAINTENANCE`/`ENTRY_TIMEOUT`/`ENTRY_DEVIATION`）・`cancel_time_condition_met`・`cancel_deviation_condition_met`（いずれも真偽値、両方独立記録することで同時発火時にどちらが要因かを後から判別可能）・`cancel_elapsed_minutes`・`cancel_deviation_pct`の5列を追加。既存のimbalance反転・メンテナンス強制キャンセルの記録にも`cancel_reason`を後付けした。これらはAI夜間議論の集計対象には一切含めていない。

**段階的導入方針**: 時間条件（60分固定）・乖離条件（SL幅の50%）ともに、まずは固定値で導入し、上記ログをもとに効果を検証してから、必要であればAI夜間調整対象への昇格を検討する。時間条件は乖離条件と役割が異なり（乖離条件が逆選択リスクを直接防ぐ主役、時間条件は静かな相場での資金拘束を防ぐ保険的位置づけ）、あえて動的化・複雑化させず固定値のまま据え置くこととした。

テスト6件追加（時間条件単独発火、乖離条件単独発火、タイムアウト系クールダウンの5分境界、imbalance系クールダウンの3秒維持確認、ログへの理由記録確認、プロファイル別閾値の独立性確認）。

#### 3.4.27 再起動時の未約定指値タイムアウト計測リセット修正（`virtual_trader.py` / `trading_engine.py`、2026-07-28実装）

**背景**: 3.4.26の実装直後に判明した副作用。未約定指値の発注時刻(`_pending_order_placed_at`)はメモリ上のインスタンス変数のみで`live_state.db`に永続化されておらず、エンジン再起動（手動停止からの復帰、クラッシュ再起動、06:00定例再起動等）を挟むと、pending状態の復元処理で`datetime.now()`により現在時刻へ引き直されていた。結果として、実際には長時間放置されていた指値でも再起動後は「経過0分」に巻き戻り、経過時間タイムアウト条件が実質機能しなくなる状態だった。

**対応内容**:
- `live_state`テーブルに`pending_order_placed_at TEXT`列を追加（新規CREATE定義・既存DBへのALTERマイグレーション両方に対応）
- `_write_live_state()`で、pending状態の指値の発注時刻をISO8601形式（`position_filled_at`と同じ形式・同じ関数系統）で保存
- 起動時の`restore_persisted_position()`で、DBの値を既存の`_parse_persisted_datetime()`でパースし、成功時はその時刻を`_pending_order_placed_at`に復元する
- **値が欠損している、またはパースに失敗した場合は`_pending_order_placed_at`を`None`のままとし、`datetime.now()`へのフォールバックは行わない**。この場合、経過時間タイムアウト条件（3.4.26）のみが判定対象外になるが、価格乖離条件・imbalance反転条件は影響を受けず引き続き機能する。復元不能を「今起きた」ことにして時間切れ判定に使うのはデータの捏造に近く、危険な放置には至らない「判定しない」を安全側の挙動として選んだ
- 上記のフォールバック発生時は、時間条件がスキップされる旨（乖離・imbalance条件は有効である旨を含む）を警告ログとして出力する

**既知の制約（未解決）**: 本カラム追加より前からpendingを抱えたまま再起動した既存環境では、初回のみ発注時刻列が`NULL`のため経過時間タイムアウトがスキップされる（乖離条件・imbalance反転条件は有効）。新規エントリー発生以降は正しく発注時刻が記録されるため、一時的な既知の挙動として対応不要と判断した。

テスト4件追加（永続化→復元後に正しい経過時間で時間タイムアウトが発火することの確認、欠損／不正値で`None`のままになり`datetime.now()`が使われないことの確認、レガシーDBへのカラムマイグレーション確認、他）。

#### 3.4.28 ダッシュボードのPowerShell無条件呼び出しのDocker/Linux対応（`btc_trading_tool/dashboard.py`、2026-07-29実装）

**背景**: フェーズ2の隔離環境（`docker-compose.real-test.yml`）で緊急停止後に「▶ 再開」ボタンを押したところ、`FileNotFoundError: 'powershell.exe'`が発生した。`_launch_ensure_engine_running()`がOS判定なしに`powershell.exe`を無条件で呼び出す設計だったため、`ensure_engine_running.ps1`をバックグラウンド起動するネイティブWindows運用向けの処理が、そのままDocker/Linuxコンテナ内でも実行されてしまっていた。同じ設計パターンの`_launch_nightly_review()`（AI夜間レビューの手動リトライボタン、6.4参照）にも同種の問題があったため、あわせて対応した。ダッシュボードのこれらのボタンを使う限り、VPS本番のDocker環境でも同じ事象が起きる構造的な問題だった。

**対応内容（`_launch_ensure_engine_running()`・`_launch_nightly_review()`の両方に同一パターンで適用）**:
- `manual_stop.flag`の削除処理（`_launch_ensure_engine_running()`側）はOS判定と独立して常に先に実行する（変更なし）
- 両関数に`platform.system() == "Windows"`判定を追加し、Windows以外では対応するPowerShellスクリプト（`ensure_engine_running.ps1`／`run_nightly_review.ps1`）の起動をスキップして`False`を返す
- 非Windows環境では、それぞれの操作が行えない旨と、対応するコンテナ（engine／ai_review）側で別途実行する必要がある旨をログ（`print`、英語・絵文字なし、既存の監視スクリプト群と同じログ規約）に出力する
- ダッシュボード画面上にも同趣旨の案内（`st.info`、日本語、UIの既存文言と同じ表現規約）をそれぞれ表示する
- Windows環境での既存挙動（PowerShellスクリプト起動）・`ensure_engine_running.ps1`／`run_nightly_review.ps1`自体は変更していない

**確認方法**: 隔離環境で（1）緊急停止→「再開」ボタン押下、（2）「AI議論を再実行」ボタン押下、の両方についてエラーが出ずログ・UI双方に案内メッセージが表示されることを確認。エンジンの再開自体は`docker compose -f docker-compose.real-test.yml start engine`で、AI夜間レビューの手動起動は`docker compose run --rm ai_review ...`相当のコマンドで、それぞれ別途行う（Docker版が`manual_stop.flag`起因の停止を正常終了［exit 0］として扱い`restart: on-failure`では自動再起動しない設計であることに起因する、既知の仕様）。Docker向けのAI夜間レビュー手動起動コマンドの運用ドキュメントへの明記は未実施（9章に残タスクとして記録）。**（1）のエンジン再開手動コマンドの必要性は2026-08-02（v73）に解消済み。詳細は3.4.40参照。（2）のAI夜間レビュー手動起動は本対応の対象外のため、Docker向け運用コマンドの案内は引き続き必要**

#### 3.4.29 フェーズ2残検証：SHORT側フロー・再起動時状態復元・`_force_close_real()`側ERR-5122/5123の実機確認（2026-07-29）

隔離環境（`docker-compose.real-test.yml`）での実機確認・ログ分析により、フェーズ2の残検証項目のうち以下が確認できた。

- **SHORT側の一連フロー確認**: `realtime_trading_log_2026-07-29.csv`を分析し、SHORTのエントリー→約定、TP到達決済（板監視、`12:55:19 SELL ENTRY 10419359`→`13:06:33 BUY TAKE_PROFIT 10406855.7692`、pnl=+12.50）、SL到達決済（2件、いずれも損益計算が理論値と一致）を確認。sideの反転・損益計算いずれもLONGと対称に正しく動作しており、3.4.18の非対称会計バグはSHORT側には存在しない
- **再起動時のポジション状態復元**: SHORTポジション保有中（SL常設済み）に`docker compose restart engine`を実行し、再起動後の`live_state.db`が`position_side`・`entry_price`・`size`・`sl_order_id`・`position_id`いずれも再起動前と完全に一致していることを確認。GMO側の建玉・SL有効注文も一致し、二重発注・誤ENTRYは発生しなかった
- **`_force_close_real()`側のERR-5122/5123実機検証**: LONGポジション保有中にGMO画面からSLを手動キャンセルした状態で緊急停止を実行し、`[FORCE-CLOSE: real] cancel sl orderId=...`の後、リトライ・CRITICAL通知に落ちることなく成行決済（LONG→SELL反転）まで正常完了することを確認。ただし`_force_cancel_maintenance()`側（3.4.21）と異なり、このログ文言だけでは「エラーコードを検知した上でベニン処理したのか、単に無条件で次工程へ進む設計なのか」を判別できない（生の`message_code`はログに出ない）。挙動としての安全性は確認済みだが、検知経路の厳密な確証までは取れていない
- **副次的に発見した既知の制約（低優先度）**: 起動時のポジション復元処理は、建玉が存在するケースの突き合わせは行うが、pendingエントリー注文がGMO側で実際にまだ有効かどうかは能動的に確認していない。あるテストの副産物として、既にGMO側では存在しないpendingエントリーがエンジン再起動をまたいで内部状態に残り続けた事例があったが、実害はなく、後続の緊急停止操作で`_force_cancel_maintenance()`のERR-5122/5123ベニン処理により正常に解消された。3.4.26のタイムアウト機構（最大60分）や板の動きでも自然に解消される設計のため対応は見送り、9章に低優先度の気づきとして記録する
- **エンジンログのバッファリングによる可視性の問題（低優先度）**: `docker compose restart`直後、新しいプロセスの標準出力が`docker compose logs`にしばらく反映されない現象を確認。`live_state.db`の`updated_at`を確認したところエンジン自体は正常稼働しており、ハングではなくPythonの標準出力バッファリングによる表示上の遅延と判明（`PYTHONUNBUFFERED=1`未設定が原因と推測）。実害はないが、VPS本番でのトラブルシューティング時に「ログが見えない＝原因不明」と誤認するリスクがあるため、9章に低優先度の残タスクとして記録する

#### 3.4.30 手数料の実測値反映（`btc_trading_tool/virtual_trader.py`、2026-07-29実装）

**背景**: `realtime_trading_log`の`fee`/`pnl`は、9章に「保留」として記録していた通りconfig.jsonの手数料率設定から計算した理論値のみで、実際の約定手数料を反映していなかった。GMOの取引履歴を確認したところ、この口座・銘柄では実際の手数料が0円である一方、理論値では4円前後を見込んでおり乖離が生じていた。この乖離は`cumulative_pnl`・勝率等の内部集計、および毎日Xに自動投稿している公開レポートの数値にもそのまま反映され続けていた。実資金移行が進み実約定が発生するようになったため、保留を解除して対応した。

**対応内容（2段階で実施）**:
- 第1段階: ENTRY（`_apply_entry_execution_unlocked`）・SL決済（Private WS `executionEvents`経由、`_apply_exit_execution_unlocked`→`_settle_real_exit_from_execution_unlocked`）について、`trading_mode=="real"`のときはWSイベントの`fee`フィールドから実手数料を取得して使用するように変更。取得できない場合は既存の理論値計算にフォールバックし、real modeのときのみ警告ログを1行出力する
- 第2段階: 板監視TP（`_execute_real_board_take_profit_unlocked`）・緊急停止時の強制決済（`_force_close_real()`）は、成行注文（`gmo_close_order`）を使うためWSイベントから直接`fee`を得られない。新規に`gmo_fetch_order_execution_fee()`（`GET /v1/executions?orderId=...`、GMO仕様上`orderId`指定の約定照会は`/v1/latestExecutions`ではなく`/v1/executions`であることが実装時に判明し訂正）を追加し、決済確認後にこの関数で実手数料を取得して同様に反映する。`_force_close_real()`は複数建玉を同時決済するケースを想定し、複数の決済注文の手数料を合算する。取得失敗（レスポンス空・`fee`欠損・例外）時は決済処理自体には一切影響させず、既存の理論値フォールバックに委ねる
- 仮想モードの計算方法・ENTRY時に即時反映される`jpy_balance`の理論値ベースの`balance_delta`計算には、いずれの段階でも変更を加えていない

**既知の残課題（低優先度）**: ENTRY時に即座に`jpy_balance`へ反映される`balance_delta`（`_enter_long`/`_enter_short`で計算）は、発注時点でまだ実際の手数料が分からないため理論値のまま計算される。約定確定時（`_apply_entry_execution_unlocked`）に実手数料が判明しても、この時点で反映済みの`jpy_balance`への差分修正は行われない。理論値と実値の差分はわずかであり、5分間隔で実行される口座残高整合性チェック（3.4.2、GMO実口座との突き合わせ）で吸収される範囲のため、対応は見送り9章に記録する

**確認方法**: 単体テスト93件がパス。実機での実手数料取得の成功率（`GET /v1/executions`への反映タイミングのラグにより、決済確認直後だとまだ取得できず理論値フォールバックになる可能性がある）は、次回以降の実弾TP・緊急停止決済発生時にログの`[WARN] ... actual fee unavailable`の有無で確認する（未実施）

---

#### 3.4.31 real modeでpending指値のキャンセル判定がearly returnで無効化される問題の修正（`btc_trading_tool/virtual_trader.py`、2026-07-30実装）

**背景**: real-test環境でSHORT指値エントリー中に価格が指値を大きく下回って動いたにもかかわらず、imbalance反転・価格乖離・タイムアウト（3.4.26）いずれのキャンセル条件も発動しない事象が実地で見つかった。Cursorによる調査の結果、`_check_pending_fill()`内の分岐構造そのものに原因があることが判明した。

**原因**: `_check_pending_fill()`は板タッチによる仮想約定判定`filled`を算出したうえで、`if filled: if self.trading_mode == "real": return`という構造になっており、real modeで`filled`が真になった瞬間からキャンセル判定（`else`側）に一切到達しなくなっていた。real modeでは約定確定をPrivate WSの`executionEvents`に一本化する設計自体は妥当だが、「板タッチ＝filled」を根拠にキャンセル判定まで丸ごとスキップしてしまう構造だったため、一度filledが真になる状態（価格が指値を超えて動いた状態）が続く限り、3.4.26のタイムアウトキャンセル機構を含むすべての安全装置が機能しなくなっていた。3.4.26はそもそも「未約定の指値が長時間放置された末に市況急変時に飲まれる」というインシデントを受けて作られたものであり、本バグは市況急変時にこそ発動してその安全装置を無効化する構造になっていた。

**対応内容**:
- 「real modeでは板タッチ仮想約定シミュレーションをスキップする」制御と、「キャンセル条件を評価する」制御を分離。`if filled and self.trading_mode != "real":`で仮想約定処理のみをgateし、キャンセル判定（imbalance反転・価格乖離・タイムアウト）はfilledの真偽に関わらずreal modeでは常に評価するように変更した
- LONG/SHORT両方で対称に修正
- 上記修正により、real modeでは価格が指値を超えて動いている間、毎tickキャンセルが試行され得るようになる。この際、既存の`_cancel_order()`はreal modeで`_cancel_real_entry_order_or_adopt_fill()`を経由しており、キャンセル試行と同時にGMO側で約定していた場合（`ERR-5122`/`ERR-5123`受信時）は`adopted_fill`として建玉に取り込まれ、誤ってFLATへクリアされないことを確認済み（差し替え不要）

**確認方法**: 追加テスト（SHORT/LONGで板タッチ＋乖離→`ENTRY_DEVIATION`キャンセル、キャンセル競合＝ERR-5122→adopt-fillでFLATにしない、板タッチだが条件未達→pending維持しWS約定で建玉化）を含む関連テスト98件がパス。real-test環境でイメージ再ビルド後、SHORT/LONG双方について板タッチ＋乖離で`ENTRY_DEVIATION`キャンセルされることをコンテナ内スモークで実機確認済み。

#### 3.4.32 pending中の総資産計算が建玉評価を含めてしまう問題の修正（`btc_trading_tool/portfolio_metrics.py` / `virtual_trader.py` / `trading_engine.py`、2026-07-30実装）

**背景**: real-test環境でSHORT/LONGいずれもpending指値中（GMO側に有効注文・建玉なし）に、板価格の上下だけでダッシュボードの総資産金額が変動する事象が実地で見つかった。3.4.31の修正とは別経路の原因であることをログで確認済み（該当指値はimbalance反転で正常にキャンセルされておりpending残留はしていなかった）。

**原因**: ダッシュボードの総資産表示が参照する`compute_total_assets`/`compute_total_assets_from_live_state`（`portfolio_metrics.py`）が`position_is_pending`を考慮しておらず、`position_side`にLONG/SHORTが入っていれば（pendingでまだ未約定であっても）`(mid - entry) * size`等の評価損益をそのまま加算していた。一方、口座整合性チェック用の`get_internal_account_state()`（`comparable_equity_jpy`）はpendingを正しく除外していたため、表示側とエンジン内部照合側で「pending中の評価」の定義がずれていた。

**対応内容**:
- `portfolio_metrics.compute_total_assets`/`compute_total_assets_from_live_state`: `position_is_pending`が真の場合、ポジション評価を行わず`jpy_balance`のみを返すよう変更（`get_internal_account_state`の`comparable_equity_jpy`と同一定義に統一）
- `VirtualTrader.unrealized_pnl`: `self.position.is_pending`が真の場合は0を返すよう変更（`total_assets`もこれに追従）
- `trading_engine.py`起動時の`last_total_assets`復元処理（口座整合性チェックの比較基準値算出）にも`position_is_pending`を渡すよう追従
- `trading_mode`（real/virtual）で分岐せず、両モードで一律pending除外とした

**確認方法**: pending中にmid価格が変動しても総資産（`jpy_balance`のまま）が変化しないことを確認する回帰テストを追加し、`compute_total_assets_from_live_state`との整合、`unrealized_pnl`が0を返すことも含めて関連テストがパス。real-test環境でイメージ再ビルド後、pending中に板が動いても総資産表示が`jpy_balance`のまま変わらないことをコンテナ内スモークで実機確認済み。

#### 3.4.33 real-test環境のエンジンログ永続化・`PYTHONUNBUFFERED`設定（`scripts/process_launcher.py` / `scripts/run_engine_with_file_logs.py` / `docker-compose.real-test.yml`、2026-07-30実装）

**背景**: real-test環境では、緊急停止後にダッシュボードの「▶ 再開」を押しても再開できず（3.4.28、Docker/Linux環境の既知の仕様）システム再起動が必要になる。この案内に従って`docker compose down`→リビルド→再起動という運用を取っていたところ、`docker logs`にしか出力していなかったエンジンの標準出力・標準エラーがコンテナ削除のたびに失われ、サーキットブレーカー（3.4.1）発動時刻の特定・原因調査ができない状態になっていた（`log-real-test/`配下にファイル出力の仕組みがなく空だった）。

**対応内容**:
- `scripts/process_launcher.py`に`attach_engine_file_logs()`を新設。現在プロセスの`sys.stdout`/`sys.stderr`を、元のストリームとファイルの両方へ書き込む`_TeeTextIO`でラップする。ファイル名はネイティブ運用と同一の命名規則（`engine_YYYY-MM-DD.log`/`engine_YYYY-MM-DD.err.log`）に揃え、追記モード（既存ログを切り詰めない）で開く
- `scripts/run_engine_with_file_logs.py`を新設。Dockerのフォアグラウンド起動用エントリポイントとして`attach_engine_file_logs()`を呼び出した後に`trading_engine.main()`を実行する
- `docker-compose.real-test.yml`のengineサービスの起動コマンドを`python -u scripts/run_engine_with_file_logs.py`に変更し、`environment`に`PYTHONUNBUFFERED=1`を追加（9章に低優先度で記録していた標準出力バッファリングによる可視性低下も解消）
- `tests/test_process_launcher_file_logs.py`を新規追加（ファイル名の命名規則、stdout/stderrのtee出力、既存ログを切り詰めず追記することの3点を確認、計3件）

**確認方法**: 上記3件のテストがパス。real-test環境をイメージ再ビルド・再作成し、反映済み。

**未対応の確認事項（9章参照）**: VPS本番用`docker-compose.yml`側のengineサービスにも同様の欠落（ログ永続化なし、起動コマンドは`python trading_engine.py`のまま・`PYTHONUNBUFFERED`未設定）があることを確認したが、本番はまだ実機未検証のフェーズのため今回は対応せず、9章に残タスクとして記録した。

#### 3.4.34 仮想モードの手数料理論値を実測に合わせて0に変更（`btc_trading_tool/virtual_trader.py`、2026-07-30、手動編集）

**背景**: 3.4.30でGMOの実際の手数料を確認したところ、この口座・銘柄では少額資金での取引のため実手数料が0円だった。仮想モード側は少額資金でのテスト条件を仮想的に再現している一方、手数料は理論値計算のまま残っており、この口座の実測（0円）と乖離していた。

**対応内容**: `virtual_trader.py`の`TAKER_FEE_RATE`定数を`0.0`に変更した。Cursorを介さず手動で編集している。

**既知の残課題（9章に記録）**: この変更はCursorによるレビュー・テスト実行を経ていないため、既存テストへの影響（手数料を前提にした損益計算のテスト等）は未確認。また、将来資金規模が大きくなり実手数料が0円でなくなった場合は、この値を実態に合わせて再設定する必要がある。

#### 3.4.35 imbalance反転キャンセル後クールダウンの同一方向判定への変更（`btc_trading_tool/virtual_trader.py`、2026-07-30実装）

**背景**: real-testで発注レート超過によるサーキットブレーカーが1日に複数回（09:46・10:56・11:42×2・11:44・11:48・11:54・12:00）発動する事象が続いた。`log-real-test/engine_2026-07-30.log`（3.4.33で永続化）と`realtime_trading_log`を突き合わせたところ、発動直前60秒間はいずれも`CANCEL_ORDER`（`cancel_reason=IMBALANCE_REVERSAL`）のみが6件以上並び、`ENTRY`の記録は0件だった。当初この事実からサーキットブレーカーのカウント対象に関する仮説を立てたが、Cursorの調査により、real modeでは指値発注成功時点（GMOへの発注成功、約定前）でカウンター（`record_order_event`）が加算される一方、`realtime_trading_log`の`reason=ENTRY`は約定確定時にしか記録されないため、未約定のままキャンセルされたケースはCSV上`CANCEL_ORDER`しか残らないことが判明した（矛盾ではなく仕様の理解不足だったと判明）。実際の発動直前は、imbalance反転によるキャンセル後、板に追従してわずかに価格が変わりながら短時間で再エントリー（`[REAL-ORDER]`）が繰り返されており、既存の同一価格再エントリークールダウン（3.4.7、3秒）が価格のわずかな変化ですり抜けられていたことが直接原因と特定した。

**対応内容**:
- `_should_block_entry_by_cancel_cooldown()`: imbalance反転（`cancel_reason`が`CANCEL_REASON_TIMEOUT`/`CANCEL_REASON_DEVIATION`以外）によるキャンセル後は、価格が異なっていても**同一方向であればクールダウン対象**とするように変更（`require_same_price`の判定から imbalance反転を除外）
- `ENTRY_COOLDOWN_AFTER_CANCEL_SEC`を3秒から5秒に延長 → **2026-08-21、10秒へ再延長（3.4.54）**
- timeout/deviationによるキャンセル後のクールダウン（同一方向かつ同一価格、`ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC`＝300秒）、メンテナンス強制キャンセルの扱い（クールダウンなし）には変更を加えていない
- `_cancel_order()`側でキャンセル時に記録する`_last_cancel_by_side`（方向をキーとした価格・時刻・秒数・キャンセル理由のタプル）の構造自体は既存のものを踏襲

**確認方法**: `test_virtual_trader.py`にテストを追加。同一方向・価格違いでのブロック（新挙動）、timeout/deviationは従来通り価格が違えば通ること、方向ごとの独立性、5秒境界値の挙動を確認。real-test環境で再ビルド・反映済み。

#### 3.4.36 real modeでの指値発注時点のログ記録`ENTRY_PENDING`（`btc_trading_tool/virtual_trader.py`、2026-07-30実装）

**背景**: 3.4.35の原因調査の過程で判明した通り、real modeでは`realtime_trading_log`の`reason=ENTRY`が約定確定時にしか記録されず、指値発注を行った時点（GMOへの発注成功時点）の記録が残っていなかった。未約定のまま短時間でキャンセルされたケースを事後調査できるようにするため対応した。

**対応内容**: `_enter_real_limit()`で、GMOへの指値発注が成功した時点（`_on_order_placed()`によるレート制限カウンター加算より前）に、`reason=ENTRY_PENDING`として`realtime_trading_log`へ記録するようにした。約定確定時に記録される既存の`reason=ENTRY`の内容・タイミングには変更を加えていない。発注自体が失敗した場合（`gmo_order`が例外）は、`ENTRY_PENDING`記録前にreturnするため記録されない（従来通り）。

**確認方法**: `test_real_entry.py`にテストを追加。発注成功時に`ENTRY_PENDING`が1件だけ記録されること、その後約定した場合に`ENTRY_PENDING`→`ENTRY`の2行がこの順でCSVに残ること、発注失敗時はいずれも記録されないことを確認。

#### 3.4.37 real modeのTAKE_PROFIT決済における実約定価格の反映（`btc_trading_tool/virtual_trader.py`、2026-07-30実装）

**背景**: GMOの実際の取引履歴とダッシュボードの記録を突き合わせたところ、real modeの板監視TP（`_execute_real_board_take_profit_unlocked()`、GMOへの成行決済）で記録される約定価格が、実際の約定価格ではなく常に目標値（`exit_price_target`）に固定されていたことが判明した。成行注文である以上、実際の約定価格は目標値より良いことも悪いこともあり得るため（実データでも両方向のズレを確認）、記録される決済損益が実態と一致しないケースがあった。

**対応内容**:
- 新規関数`gmo_fetch_order_execution_fill(order_id)`を追加。`GET /v1/executions?orderId=...`から数量加重平均の約定価格と手数料合計をあわせて返す（戻り値`(avg_price, total_fee)`、取得不可時は該当要素が`None`）。既存の`gmo_fetch_order_execution_fee()`（3.4.30）はこの関数のfeeのみを返す薄いラッパーに変更し、`_force_close_real()`側の既存呼び出しは無変更で動作する
- `_execute_real_board_take_profit_unlocked()`: 決済確定後に`gmo_fetch_order_execution_fill()`で実約定価格を取得し、有効な値が取れた場合はそれを`fill_price`として`_settle_real_exit_from_execution_unlocked()`に渡す。取得できない場合は従来通り目標値（`exit_price_target`、無効な場合は板の最良気配）にフォールバックし、`[WARN]`ログを出力する
- STOP_LOSS（Private WebSocketの`executionEvents`経由、元々実際の約定価格を使用）、virtual modeのシミュレーション計算（目標値を約定価格とみなす既存ロジック）、`_force_close_real()`（FORCE_CLOSE経路）には変更を加えていない

**確認方法**: `test_real_tp_sl.py`にテストを追加。実約定価格取得時にそれが使われること、取得失敗時に目標値へフォールバックしWARNログが出ること、`gmo_fetch_order_execution_fill()`の加重平均計算・異常系（空レスポンス・例外）、STOP_LOSS/virtual modeの既存挙動が変化していないことを確認。real-test環境で再ビルド・反映済み。

---

#### 3.4.38 監視・レポート系5スクリプトのvenv参照方式統一（`scripts/run_*.sh`、2026-07-31実装）

**背景**: v67で`run_public_report.sh`・`run_sns_report.sh`の2本が`python`コマンドをそのまま呼び出しており、VPSホスト側venv（`.venv-host`）を参照していない点が9章に残課題として記録されていた。修正をCursorに依頼したところ、レビューの過程で、4.5.8で導入した`run_anomaly_check.sh`・`run_csv_consistency_check.sh`・`run_check_monitor_heartbeats.sh`の3本も**実際にはvenvを参照していなかったこと**が判明した。v67時点の記述はこの3本が既にvenv参照済みという誤った前提に基づいていたため、あわせて訂正する。

**対応内容**: 5本すべてに`PYTHON_BIN="$PROJECT_ROOT/.venv-host/bin/python"`を定義し、本体スクリプトの実行・Telegram失敗通知処理の両方で`$PYTHON_BIN`を使う形に統一した。systemd経由の実行はシェルのvenv activateを経由しないため、この明示パス参照によりVPSホストに`python`という名前のコマンドが存在しない場合や依存ライブラリがvenv側にしか無い場合でも動作するようになる。

**確認方法**: 5本それぞれで`bash -n`による構文確認を実施。改行コードはLF（既存の`.gitattributes`設定に従う）。実機（VPS）での動作確認はVPS構築時に他セットとまとめて実施する。`VPS構築手順書.md`2.1節のチェックリストを更新済み（9章の該当残タスクは解消）。

---

#### 3.4.39 発注用APIキー排他ロックのflock化とWindowsネイティブでのreal mode起動拒否（`trading_engine.py`、2026-07-31実装）

**背景**: real-test環境でWindows Update起因のホスト強制再起動が発生した際、`[ALERT] real mode duplicate start blocked / existing_pid=1`のエラーで再起動がブロックされる実害が発生した。原因を調査したところ、3.4.22の排他ロック（`runtime/gmo_trade_key.lock`、`os.kill(pid, 0)`によるPID生存確認方式）が、Dockerコンテナ特有の性質と相性が悪いことが判明した。コンテナのメインプロセスはinitを挟まない限りほぼ確実にPID 1になり、かつコンテナ再起動のたびにPID名前空間が1から採番し直される。そのため、グレースフルシャットダウンを経ずにコンテナが落ちた場合（Windows Updateの強制再起動、クラッシュ等でSIGKILL相当になるケース）、staleなロックファイル（`pid: 1`）が残った状態で次回起動時に新しいプロセスもまたPID 1になり、`os.kill(1, 0)`が「自分自身の生存確認」になってしまい常に「生存」と誤判定していた。

**対応内容**:
- 排他判定を`os.kill(pid, 0)`によるPID生存確認から、`fcntl.flock(LOCK_EX | LOCK_NB)`によるOSレベルのアドバイザリロックに変更（`acquire_gmo_trade_key_lock()`）。ロック取得に使ったファイルディスクリプタ（`_gmo_trade_key_lock_fd`）はプロセス終了まで開いたまま保持する。`flock`はロック保持プロセスがどのような終了の仕方（SIGKILL・電源断相当を含む）をしてもOSが自動的に解放するため、PID再利用に起因する誤検知が構造的に起こらなくなる
- ロックファイルの中身（`{"pid": <int>, "started_at": "ISO8601"}`）は排他判定には使わず、診断・通知表示用のメタデータとして`_write_gmo_trade_key_lock_fd()`経由で保持中のfdに直接書き込む（inodeは差し替えない）
- 解放は従来通り`finally`ブロック＋`atexit`の二重防御を維持（`release_gmo_trade_key_lock()`）
- `fcntl`はWindows非対応のため、関数内でのlazy importとした（Windowsでのvirtual mode起動時にモジュールimportで失敗しないようにするため）
- レビュー過程で、real modeの起動チェックにOS判定（Windowsネイティブでのreal mode起動を拒否する処理）が存在しないことが判明した。実運用のWindows側はvirtual modeのみを前提としているが、`config.json`の`trading_mode`を`real`にすればWindowsネイティブ経路でも到達しうる状態だった。また、Windows上のpytestから既存4テストが直接呼ばれており、`fcntl`導入後はそのままでは動作しない制約も判明した。これを受け、real modeをDocker/Linux専用として明文化する方針とし、`main()`内で`trading_mode == "real"`と判定した直後・ロック取得より前に`_reject_real_mode_on_windows_or_exit()`を追加した。`platform.system() == "Windows"`の場合、Telegram通知の上`SystemExit(1)`で起動を中止する
- 既存4テスト（正常取得／生存PIDでのブロック＋通知／stale削除／`SystemExit`確認）を`flock`の観点（同一プロセスの別fdが保持中／保持プロセスが強制終了済み相当）に書き直し。Windowsネイティブ拒否の新規テストを追加（`platform.system`をモックし、`SystemExit(1)`・Telegram通知呼び出しを確認）

**確認方法**: `flock`関連4件はLinux環境（Docker `gmo-btc-ws:latest`）でpytest実行、4 passed。Windowsネイティブ拒否のテストはWindows環境で実行、1 passed。real-test環境での実機確認（Windows強制再起動相当の状況の再現）は未実施（次回同様の状況が発生した際に効果を確認）。

---

#### 3.4.40 緊急停止後のエンジン待機・自動再開（PAUSED状態、`trading_engine.py` / `dashboard.py` / `check_engine_crash_loop.py`、2026-08-02実装）

**背景**: v59（717〜726行目）でDocker/Linux環境における「▶ 再開」ボタンの構造的な限界が明らかになっていた。`manual_stop.flag`検知でエンジンが安全停止する際、`sys.exit`を呼ばない自然終了（終了コード0）でプロセスが終わる設計のため、`restart: on-failure`のDockerではコンテナが自動再起動されず、再開ボタンを押してもフラグが削除されるだけでエンジンが再開しない状態が、VPS本番稼働後も未解消のまま残っていた。ネイティブWindowsでは`ensure_engine_running.ps1`が新規プロセスを起動していたが、Docker環境には同等の仕組みがなかった。

**検討した代替案**: (A) ダッシュボードコンテナにDocker socketへのアクセス権を与え、`docker compose`経由でengineコンテナを実際に再起動する案、(B) エンジン側の停止方式自体を変更し、プロセスを終了させず待機してから再開する案、の2案を比較した。(A)はDockerの強い権限をダッシュボードに与えることになりセキュリティ上のトレードオフがある一方、実装は小さい。(B)はDockerの権限を一切与えずに済み安全だが、待機・復帰処理の新規設計が必要で実装量は大きい。ダッシュボードの外部公開を予定していないことを踏まえつつも、実資金運用中のシステムであることを優先し、**(B)を採用**した。

**対応内容**:
- `manual_stop.flag`検知→ポジション決済（既存の`_force_close_real()`等は無変更）→WebSocket切断（既存のまま）の後、プロセスを終了せず`engine_status=PAUSED`として同一プロセス内で待機ループに入るよう変更
- 待機ループは5秒間隔で`manual_stop.flag`の有無をポーリング。トレード処理・WebSocket接続は待機中動かさない
- 待機中も`live_state.db`の`updated_at`を5秒間隔で更新し続け、`check_monitor_heartbeats.py`等の心拍監視が誤って「異常」と検知しないようにした
- `manual_stop.flag`が消えたことを検知したら、WebSocket再接続（起動時の初期化処理を再利用）→`engine_status`を`RUNNING`に戻す→通常ループへ復帰
- 待機時間に上限は設けず、待機開始から24時間ごとに「まだ緊急停止中です」という内容をTelegramへ再通知する（`_notify_still_paused()`。手動停止時に書き込まれる`manual_stop_reason.json`の内容をそのまま使うため、自動発動・ダッシュボードボタンいずれの停止理由も正しく表示される）
- `check_engine_crash_loop.py`は、コンテナの状態が`exited`のときのみクラッシュループと判定するロジックのため、`PAUSED`中もコンテナが`running`のままである限り誤検知しないことをコメントで明記
- `dashboard.py`側の、Docker/Linux環境で表示していた「engineコンテナ側を別途再起動してください」という案内メッセージ・`_launch_ensure_engine_running()`のPowerShell起動処理は、Docker/Linuxでは常に何もしない構成のまま維持（Windows側の既存挙動は無変更）
- `gmo_trade_key.lock`（3.4.39のflock方式）はPAUSED中も保持されたまま解放されないため、待機中に別プロセスが誤って起動しても二重発注の懸念はない

**確認方法**: `docker-compose.test.yml`の隔離環境で、緊急停止→`PAUSED`遷移→フラグ削除→`RUNNING`復帰の一連の動作、`PAUSED`中の`live_state.db`の`updated_at`継続更新、`check_engine_crash_loop.py`・`check_monitor_heartbeats.py`のいずれも異常判定しないことを確認済み。VPS本番環境（実資金・ポジション保有中）にも反映済みで、`docker logs`上の正常動作を確認した。

---

#### 3.4.41 内部日次レポートのsystemdタイマー欠落の発見・追加（`scripts/run_daily_report.sh`・`deploy/systemd/daily-report.*`、2026-08-02新規作成）

**背景**: VPS本番稼働2日目、SNS配信（`sns-report.timer`、06:15）が`daily_history.jsonl has no realized_pnl for trading_day=2026-08-01`というエラーで失敗した。調査したところ、`build_daily_report.py`（ネイティブ運用では`BTC_Daily_Report`タスク、毎日05:55に`daily_history.jsonl`へ前日分の実現損益を1行追記する処理）に対応するLinux版ラッパースクリプト・systemdタイマーが、VPS構築時に配備した9セットに一度も含まれていなかったことが判明した。Windows版（`run_daily_report.ps1`）のみが存在し、Linux/systemd向けの移植自体が漏れていた。

**対応内容**:
- `scripts/run_daily_report.sh`を、既存の`scripts/run_public_report.sh`と同じパターン（ロックファイル管理、`PYTHON_BIN`変数によるvenv明示参照、失敗時の`log/`記録とTelegram通知）で新規作成
- `deploy/systemd/daily-report.service`・`.timer`を、既存の`public-report.service`/`.timer`と同じ構成で新規作成（毎日05:55、`User=takuya`・`Group=takuya`を最初から含める）
- VPS側で配備・`enable --now`し、9セットが10セットになった

**2026-08-01分の`daily_history.jsonl`欠落への対応**: 本番切替が2026-08-01の日中〜夜間だったため、同日05:55時点ではまだVPS上に`daily-report`が存在せず、記録が失われた。取引時間が極端に短く重要度が低いため、本人の判断により**補完せず欠測日として扱う**方針とした。当日分のSNS配信・公開レポートも投稿を見送り、2026-08-03分から通常運用を再開する。

**確認方法**: VPS側で手動実行し、正常終了・Telegram送信成功を確認。ただし手動実行のタイミングが日中だったため、`trading_day_date`の判定上その時点の「当日（8/2）分」の中間集計行が`daily_history.jsonl`に追記される形になり、翌日05:55の正常実行での重複を避けるためこの行は削除して対応した。

---

#### 3.4.42 systemd 10セットへの`User=takuya`追加（`deploy/systemd/*.service`、2026-08-02実装）

**背景**: 3.4.41の調査に付随して、`log/`配下のホスト側スクリプトが書き込むログファイル（`anomaly_check_*.log`等）が`root`所有になっており、日付が変わるたびに新規ファイル作成時の権限エラー（`Permission denied`）が発生する事象が見つかった。原因は、`deploy/systemd/`配下の`.service`ファイルに`User=`の指定が無く、systemdがデフォルトの`root`としてこれらのサービスを実行していたためだった。

**対応内容**: 既存9セットすべての`.service`ファイルの`[Service]`セクションに`User=takuya`・`Group=takuya`を追加（`WorkingDirectory=`の直後）。新規作成した`daily-report.service`（3.4.41）にも最初から含めた。VPS実機側の`/etc/systemd/system/`配下には一時的に`sed`で直接適用していたが、リポジトリ側の`deploy/systemd/`テンプレートにも同じ変更をCursor経由で反映し、テンプレートと実機の乖離を解消した。あわせて`config/`・`runtime/`・`log/`のうち、既に`root`所有で作成済みだった過去分のファイルも`chown`で`takuya`に揃えた。

**確認方法**: `systemd-analyze verify`で全10セットの構文確認、`sudo -u takuya`での各スクリプトの手動実行、`engine-restart.service`変更後の`docker compose restart`が正常動作することを確認済み。

---

#### 3.4.43 起動時account integrity checkの比較基準変更（real modeのみ、`btc_trading_tool/virtual_trader.py`、2026-08-02実装）

**背景**: VPS本番稼働2日目、ポジション保有中のコンテナ再作成（3.4.40・3.4.42対応の反映のため）で`[ALERT] account integrity check failed`（`jpy_gap=5,251.01`、`tolerance_jpy=3,000`超過）が発生した。調査したところ、`expected_jpy_balance()`が使う`initial_jpy`（`config.json`の固定値、デフォルト50,000）が、実際にGMO口座へ入金した金額（55,518円）と食い違ったままだったことが直接原因と判明した。`initial_jpy`は入金額を増やすたびに手動で`config.json`を更新する必要がある設計だったが、この運用が徹底されていなかった。

**対応内容**: `check_account_integrity()`の比較基準を、`trading_mode`で分岐する形に変更した。
- **real**: `daily_start_balance + daily_realized_pnl`（3.4.8で導入済みの日次損失上限判定と同じペア。`daily_start_balance`は新しい`trading_day_date`になった最初の起動時に、その時点の`jpy_balance`＝GMOと同期済みの実残高から自動的に設定されるため、入金額が変わっても翌日06:00の再起動で自動的に基準が更新され、`initial_jpy`の手動管理が不要になる）
- **virtual**: 従来通り`expected_jpy_balance()`（`initial_jpy + cumulative_pnl`、LONGは想定元本差し引き）のまま変更なし

**方針決定の経緯**: 当初は`expected_jpy_balance()`自体の差し替えを検討したが、Cursorの事前調査で、virtual側は日またぎ時に残存するLONGポジションがある場合、新基準（`daily_start + daily_realized`）と既存の想定元本差し引きロジックが二重差し引きになり誤検知することが判明した。修正案として(1)近傍判定によるヒューリスティック、(2)`initialize_daily_loss_state()`側で日次リセット時にvirtualポジションの想定元本を補正、(3)`check_account_integrity()`内でreal限定のモード分岐、の3案が提示され、影響範囲が本来の目的（realのアラート解消）に対して最小である(3)を採用した。virtual側の日またぎ二重差し引き問題自体は実資金には影響しないため、対応を見送り9章に低優先度で記録するにとどめた。

**確認方法**: `tests/test_account_integrity_baseline.py`を新規作成し、real側の日またぎ動作（stale な`initial_jpy`のままでもアラートが解消すること）・LONGの想定元本非差し引き・virtual側が従来通り動作することを含む6件のユニットテストで確認（6 passed）。`docker-compose.real-test.yml`は起動せず、`calc_trading_day_date()`への日付モックによる代替確認とした。VPS本番環境（実資金・ポジション保有中）にも反映済みで、`docker logs`上`account integrity check`が`[OK]`になったことを確認した。

---

#### 3.4.44 監視ハートビートファイルの共通書き込み処理をファイルロック化（`scripts/monitor_heartbeat.py`新設、2026-08-03実装）

**背景**: VPS本番稼働3日目、`orphan-orders-check`から断続的に`[ALERT] wrapper exception / error=check_orphan_orders.py exit=1`が届いた。ログを精査したところ、`[Errno 2] No such file or directory: '/opt/gmo-btc-ws/runtime/monitor_heartbeats.json.tmp' -> '/opt/gmo-btc-ws/runtime/monitor_heartbeats.json'`というエラーが原因だった。各監視スクリプトが「固定名の一時ファイル（`monitor_heartbeats.json.tmp`）に書く→本番ファイルへ`os.replace`」という手順で心拍を記録していたため、5分間隔で実行される複数のスクリプト（`crash-loop-check`・`orphan-orders-check`等）の実行タイミングが重なると、片方が一時ファイルをリネームした直後にもう片方が同じ一時ファイル名を探しに行って競合していた。

**対応内容**:
- 共通モジュール`scripts/monitor_heartbeat.py`を新設し、`record_monitor_heartbeat(heartbeats_path, heartbeat_key, logger=..., now=...)`として一本化した
- 読み取り→更新→書き込みの一連を排他ロックで直列化（Unix: `fcntl.flock`、Windows: `msvcrt.locking`。`.lock`という別ファイルに対してロックを取得し、ロック取得中に本体ファイルの読み書きを行う）
- 一時ファイル名を`monitor_heartbeats.json.{pid}.{uuid4}.tmp`とし、プロセスごとに一意にすることで、たとえロックが無くても`os.replace`が衝突しないようにした（ロックと合わせた二重の安全策）
- 既存5スクリプト（`check_engine_crash_loop.py`・`check_orphan_orders.py`・`check_csv_db_consistency.py`・`check_trading_anomaly.py`・`check_engine_process.py`）すべての`_record_monitor_heartbeat()`を、この共通関数を呼ぶだけの薄いラッパーに置き換えた。Windows専用の`check_engine_process.py`も同じ関数を使うため、プラットフォーム分岐は`monitor_heartbeat.py`内に集約されている

**確認方法**: `tests/test_monitor_heartbeat_concurrency.py`を新規作成。(1) 5キーを別プロセスから同時書き込み（各40回）→例外なし・全キー残存・一時ファイル残留なしを確認、(2) `check_engine_crash_loop.py`等5スクリプトのラッパーをスレッド同時実行→全キー残存を確認。あわせて、`monitor_heartbeat.py`単体を5プロセス×60回の同時書き込みで独立検証し、エラー・欠落・一時ファイル残留がないことを確認した。VPS本番環境（実資金・ポジション保有中）にも反映済み。8/2 13:45・20:30に発生していた`ERR-5008`（タイムスタンプ遅延、VPSの時刻同期自体は`timedatectl status`で正常と確認済み）が、この競合によるI/O待ちを遠因としていた可能性があるため、再発するか経過観察とする。

#### 3.4.45 orphanチェッカーの連続検知化（`scripts/check_orphan_orders.py`、2026-08-04実装）

**背景**: 8/3・8/4と2日連続で`[ALERT] orphan GMO active order(s) detected`が発生した。いずれも該当`orderId`はGMO画面上の建玉・注文には実害が無く、原因は発注直後（数秒〜数十秒）のタイミングでorphanチェッカーが実行され、`live_state.db`へ新規注文の`entry/tp/sl_order_id`が反映される前に検知してしまうレースコンディションだった（8/3分は3.4.44に付随する調査で確認済み）。

**対応内容**:
- `runtime/orphan_orders_state.json`を新設し、直前のチェックでorphan候補だった`orderId`集合を`os.replace`によるatomic writeで保持する（`load_suspect_order_ids()`/`save_suspect_order_ids()`）
- GMOからのactive order取得・`live_state.db`との突き合わせによるorphan抽出条件（`find_orphan_orders()`）自体は変更していない
- 抽出されたorphan候補を、前回チェック時点でも候補だった（＝連続2回検知）ものと、今回初めて候補になった（＝1回目）ものに分離する`split_orphans_by_consecutive()`を追加
- 1回目はログ記録のみでTelegram通知を行わず（`status=orphan_pending`）、連続2回目で初めてTelegram通知する（`status=orphan_detected`）
- orphanが解消された（次回チェックで候補から外れた）場合は状態ファイルをクリアし、アラートは発生しない
- チェック間隔は`orphan-orders-check.timer`の既存設定（5分間隔）のまま変更していない。この方式により、真のorphan発生時もアラートまで最大1チェック間隔（約5分）の遅延が生じる点は仕様として許容する（`no auto-cancel, manual review required`という既存の運用方針上、数分の遅延は実害が小さいと判断）

**確認方法**: `tests/test_check_orphan_orders.py`に、1回目保留・連続2回目で発報・レース解消時は無発報、の3パターンを追加（既存の即時アラート前提のテストは連続2回前提に更新）。全8件pass。VPS実機（ホスト側venv、`orphan-orders-check.service`）へ`git pull`のみでデプロイし（既存のコンテナ再ビルド・エンジン再起動は不要な実行形態）、手動で1回実行して`runtime/orphan_orders_state.json`が正しく生成され`status=ok`（orphan無し）で終了することを確認済み。連続2回検知によるアラート発報自体の実地確認は、次回orphanが自然発生した際の挙動待ちとする。

#### 3.4.46 `ai-review.service`の`--profile`位置修正によるAI夜間レビュー復旧（`deploy/systemd/ai-review.service`、2026-08-04実装）

**背景**: 朝5:55の日次レポートで「夜間AI議論: 記録なし」が2026-08-02〜8-04の3日間続いていることが判明した。調査の結果、`ai-review.service`の`ExecStart`が`docker compose run --rm --profile manual ai_review ...`という書き方になっており、VPSのDocker Compose（v5.3.1）では`--profile`が`run`サブコマンドのオプションではなく`docker compose`本体のグローバルオプションであるため、`unknown flag: --profile`で起動直後に失敗していたことが判明した。`ai-review.timer`自体は毎日03:30に正常に起動していたが、`ai-review.service`側が常に失敗（exit=1）していたため、`log/ai_review_decision_YYYY-MM-DD.json`が一度も生成されず、日次レポート側の「記録なし」表示（同ファイル不在時の正規表示）につながっていた。

**対応内容**:
- `ExecStart`を`docker compose --profile manual run --rm ai_review ...`（`--profile manual`を`run`より前に移動）に修正。イメージ名・環境変数・その他の引数は変更していない
- リポジトリ側`deploy/systemd/ai-review.service`を修正・コミット・push
- VPS実機側`/etc/systemd/system/ai-review.service`へ`sudo cp`で反映し、`systemctl daemon-reload`実行後、`ExecStart`の内容と`systemctl list-timers`での次回起動予定（翌日03:30）を確認

**確認方法**: 修正直後、当日分（`target_date=2026-08-03`、既定では暦日で前日分を対象とする設計のため）を手動実行し、約1分で`status=applied`・`ai_review_decision_2026-08-03.json`生成（`outlier_rollouts=0`）を確認した。手動実行前に、config.jsonの`pending_rollouts`が空（進行中の段階適用が無い）ことを確認した上で実行している。この確認は、5.4.2のcanary方式が暦日ではなく`review_pipeline`の実行回数ベースで日数（`day_index`）を進める設計であるため、ロールアウト進行中に日中手動実行すると意図せず段階が1つ進んでしまう可能性があるという理由による（9章に運用上の注意点として記録）。翌日以降の03:30定時実行が正常に完了し、5:55の日次レポートで「夜間AI議論」に記録が出るかは経過観察とする。

---

#### 3.4.47 板TP経路のbenign cancel時建玉確認・closeOrder後settle例外分離（`btc_trading_tool/virtual_trader.py`、2026-08-03設計・実装完了、VPS反映済み）

**背景**: 2026-08-03 06:03:12、板TP（REAL-TP）のSL取消→成行`closeOrder`はGMO側で成功していたが、直後に発生したERR-5008（`WebSocketManager`コールバック内の例外、タイムスタンプ遅延によるもの）により内部側のsettle/confirm処理が完走しなかった。その後のSLキャンセルリトライはすべてERR-5122（取消済み/約定済みを示すbenignコード）としてsettleも成行決済も行わずreturnし続け、内部ポジションが宙に浮いたまま約7分間放置された結果、`reconciliation_mismatch`で`manual_stop.flag`が自動発動した。「ERR-5122/5123はbenignなので無視してよい」という既存設計（3.4.20・3.4.29参照）自体は正しいが、内部に決済すべきポジションが残っている状態でこれが働いたため、今回は復旧の妨げとなった。

**対応内容**（対象は板TP経路のみ。imbalance反転・メンテナンス強制・タイムアウト経由のキャンセル処理には変更なし）:

1. **benign cancel時の建玉確認分岐**（`_execute_real_board_take_profit_unlocked`内）: SLキャンセルがERR-5122/5123を受けた際、従来は即`return`していたが、`fetch_open_positions()`でGMO側の実際の建玉をpositionId突き合わせで確認するよう変更した。
   - 建玉が既にフラット（該当positionIdなし） → 成行決済を行わず内部settle（KPI/CSV記録）を実行
   - 建玉がまだ残存 → 従来通りMARKET close処理へfall-through
   - openPositions自体の取得に失敗 → CRITICALアラートして中断（サイレント失敗を避ける）

2. **closeOrder受理後のconfirm/settle例外分離**: `closeOrder`成功ログ直後の処理を、独立したtry/exceptによる最大3回のリトライループ（`_BOARD_TP_SETTLE_MAX_ATTEMPTS = 3`、`_BOARD_TP_SETTLE_RETRY_SEC = 1.0`）に変更した。コールバック側で例外（今回のERR-5008相当）が発生してもsettleが打ち切られず再試行される。リトライ判定には`self.position.side is None`（`_settle_real_exit_from_execution_unlocked`が決済完了時に必ずクリアする内部状態）を用いており、直前の試行で既にsettle済みだった場合の二重settleを防いでいる。全試行が失敗した場合のみ`[CRITICAL] REAL MODE BOARD TP SETTLE FAILED`としてTelegramへ即時アラートする（旧`CONFIRM FAILED`文言はこちらに統合）。close後の共通後処理は`_finalize_real_board_tp_settle_unlocked()`として切り出した（フラットsettle・close後settle双方から呼び出し、重複回避）。

**テスト**（`test_real_tp_sl.py`、既存分含め9 passed）:

| テスト | 検証内容 |
|---|---|
| `test_board_tp_benign_sl_cancel_flat_settles_internal` | benign cancel + 空建玉 → 内部settle、成行決済なし |
| `test_board_tp_benign_sl_cancel_with_open_position_continues_close` | benign cancel + 建玉残存 → MARKET close続行 |
| `test_board_tp_settle_survives_exception_after_close_accepted` | close受理後に例外発生 → リトライでsettle完走 |
| `test_board_tp_settle_exhausted_alerts_critical` | 全試行失敗 → CRITICAL、ポジションは未クリアのまま残存（二重決済防止のフェイルセーフ側に倒す設計） |

**レビュー結果**: 実装差分・テストとも行単位で確認し、意図通りの分岐・スコープであることを確認済み。唯一の留意点として、confirm/settleの外側3回リトライは内部の`_confirm_real_position_closed_unlocked`自体が持つ3回×1秒のリトライと合わさるため、CRITICAL到達までの最大所要時間が旧実装の即時から約10秒程度に伸びている。正しくsettleすることを優先した結果であり許容範囲と判断した。

**別途調査（付随）**: ERR-5008の原因仮説として挙がっていたVPSクロックドリフトについてもSSHで確認し、`ntpsec`稼働・オフセットms単位で大きなズレなしを確認（この所見は3.4.46直前のv75変更点本文に記載のNTP調査と同一）。

**状態**: 2026-08-06、VPS実機への反映状況をCursor経由で調査した結果、既に反映済みであることを確認した。該当コミット（`c618bcb`、2026-08-03。SNSリール動画対応コミットに同梱される形でpull済みだった）がVPSリポジトリのHEADの祖先として存在し、稼働中の`btc-engine`コンテナ内`virtual_trader.py`にも上記1・2の分岐・リトライループが実在していることを、コンテナ内ファイルとVPSリポジトリのSHA-256完全一致により確認した（イメージ作成2026-08-04 23:06 JST）。新たなデプロイ作業は発生していない。残るのは、次回実際に板TPが発動した際のログで`SETTLE FAILED`のCRITICALが出ていないことの動作確認のみ（9章参照）。

---

#### 3.4.48 板TP実約定価格取得の失敗ログ可視化・APIキー権限不備の解消・過去分の遡及修正（`btc_trading_tool/virtual_trader.py`ほか、2026-08-04〜05実装）

**背景**: ダッシュボードの当日損益とGMO取引履歴の合計金額が一致しない事象を調査した結果、板監視TAKE_PROFIT決済で`gmo_fetch_order_execution_fill()`による実約定価格の取得が常に失敗し、`pos.exit_price_target`（理論上のTP目標値）がpnlとして記録され続けていたことが判明した。原因の特定のため、まず`gmo_fetch_order_execution_fill()`の例外握りつぶし（`except Exception: return None, None`）をやめ、失敗理由（空レスポンス／価格欠損／手数料欠損／例外）を区別してログ出力するようにし、あわせて`_finalize_real_board_tp_settle_unlocked()`側で初回失敗時に1.5秒待って1回だけリトライする処理を追加した（`_BOARD_TP_FILL_FETCH_RETRY_SEC = 1.5`）。

このログ可視化により、実際の失敗理由が`GMO API error status=1 messages=[{'message_code': 'ERR-5012', 'message_string': 'Invalid API-KEY, IP, or permissions for action.'}]`であることが判明した。取引用GMO APIキー（`GMO_API_KEY_TRADE`）に「約定情報取得」権限が付与されておらず、`/v1/executions`への照会が常に拒否されていたことが根本原因だった。GMO側の管理画面で当該権限を追加したことで解消し、修正後の板TP決済では実約定価格の取得に成功している。

**過去分の遡及修正**: この権限不備は実資金運用開始からログ可視化のデプロイ（2026-08-04 00:01頃）まで存在していたと考えられたため、2026-08-01〜2026-08-05の期間で理論値フォールバックが発生した板TP決済を`engine_*.log`から全55件抽出し、GMOの`/v1/executions`（少なくとも約3.5日前まで遡って照会可能なことを確認済み）から実約定価格を再取得して影響を確認した。55件全ての実際のpnl合計は、記録済みpnl合計より+188.87円多かった（帳簿が利益を過小に計上していた）。勝敗判定（pnlの符号）が変わるケースは無かった。手数料はAPI参照上いずれも0円で、価格差し替えのみで差分が説明できた。

この55件について、以下の遡及修正を実施した（事前にバックアップを`.bak_20260805`として保存）。

- `realtime_trading_log_2026-08-0[1-5].csv`: 該当55行のprice・pnl列を実約定値に更新し、修正行以降の`cumulative_pnl`列を日付順・ファイル横断で再計算（140行に影響）
- `daily_history.jsonl`: 影響を受けた2026-08-02〜2026-08-04の`realized_pnl`を更新（2026-08-01は該当行が無いため新規作成せず据え置き）
- `live_state.db`: `cumulative_pnl`に+188.87円を反映。`daily_realized_pnl`（06:00起点の当日カウンタ）・`jpy_balance`・`total_gross_win`等は変更していない。55件のうち2026-08-05分（00:00〜05:39発生、権限修正前）は取引日としては2026-08-04扱いのため、修正時点の当日（2026-08-05 06:00以降）分の`daily_realized_pnl`には影響が無い

なお、実際の口座残高（`jpy_balance`）は元々GMO側の実残高に定期同期される値のため、この記録上の誤りによる影響を受けておらず、実資金は常に正しかった。

**運用上の注意点（新規判明）**: エンジン稼働中に`live_state.db`を直接書き換えても、エンジン自身が毎秒DBを上書きするため変更が定着しない。手動でのデータ修正が必要な場合は、ポジションがフラットであることを確認した上でエンジンを停止し、DB更新後に再起動して定着させる必要がある。

**SNSへの反映**: 2026-08-04分の日次SNSレポート（Instagram・Threads）は、上記修正が反映される前は未配信の状態だったため、修正後の`daily_history.jsonl`から生成し直してTelegramへ配信した。2026-08-03分以前の、既に配信済みのSNS投稿内容は訂正していない（影響額が小さく、内部記録・現在のカウンタの正確性を優先する方針とした）。

**追加対応（2026-08-07）**: `gmo_fetch_order_execution_fill`の失敗が連続N回発生した場合にアラートする仕組みを実装・VPS反映した（3.4.49参照）。

---

#### 3.4.49 板TP実約定価格取得の連続失敗アラート（`btc_trading_tool/virtual_trader.py`、2026-08-07実装・VPS反映済み）

**背景**: 3.4.48で対応したTP実約定価格取得バグ（APIキー権限不備）は、約1ヶ月間、理論値フォールバックが静かに継続する形で気づかれなかった。同種の障害を早期検知するため、`gmo_fetch_order_execution_fill()`の連続失敗を能動的にアラートする仕組みを追加した。

**対応内容**: `gmo_fetch_order_execution_fill()`を`(avg_price, fee, fail_reason)`の3値タプル返却に変更（成功時は`fail_reason=None`。価格は取得できたが手数料のみ欠損したケースは失敗扱いにしない）。板TP経路（`_finalize_real_board_tp_settle_unlocked()`）専用の連続失敗カウンタ`_board_tp_fill_fetch_fail_streak`を追加し、取得成功でリセット、失敗が3の倍数（3・6・9…）に達するたびにTelegramへ`[CRITICAL] REAL MODE BOARD TP FILL FETCH FAILED`を送信する（一度送ったら以後サイレンスしない設計）。失敗理由（空レスポンス／価格欠損／手数料欠損／例外）・連続回数・`close_order_id`をアラート文に含める。SL/imbalance/強制決済等、板TP以外の決済経路には影響しない。`close_oid=None`となるbenign cancel時の内部settle（既にフラットでcloseOrder自体を呼ばない経路）は、実約定価格取得の対象自体が存在しないため、このカウンタの対象外。

**テスト**: `test_board_tp_fill_fetch_alert.py`（新規）ほか関連11件、`tests/`全体249 passed。3回連続失敗で1回アラート、成功でリセット、リセット後の再アラート、6回・9回目の追加アラート（サイレンスしないことの確認）、手数料のみ欠損時はカウントしないことを確認。

**反映**: 建玉フラット確認後、`git pull`（`c10c001→681a808`）→engineサービスのみ`docker compose build`→`up -d --no-deps --force-recreate`で反映。リポジトリとコンテナ内ファイルのSHA-256一致を確認済み。

---

#### 3.4.50 GMOプライベートAPI呼び出しの診断ログ追加（ERR-5008切り分け用、`btc_trading_tool/virtual_trader.py`、2026-08-07実装・VPS反映済み）

**背景**: ERR-5008（GMOタイムスタンプずれエラー）の根本原因が7.26時点でも未確定のまま残っていた。VPSのクロックドリフトは既に否定済み（3.4.46直前の本文参照）で、残る仮説は(1)署名用タイムスタンプ生成〜HTTP送信までの遅延、(2)GMO API側のタイムスタンプ許容窓の狭さ、の2つだった。次回発生時にこれを切り分けられるよう、診断ログを追加した。

**対応内容**: `_gmo_private_request()`に`time.monotonic()`による計測を追加した。
- `sign_to_send_ms`: 署名用タイムスタンプ生成〜HTTP送信直前までの遅延
- `rtt_ms`: 送信〜レスポンス受信までの往復時間

レスポンスの`messages`に`ERR-5008`が含まれる場合（HTTPエラー・通常のstatus!=0エラーどちらの経路も対象）にのみ、`[ERR5008-DIAG] caller=... endpoint=... api_timestamp=... sign_to_send_ms=... rtt_ms=... response_date=...`という1行の診断ログを`print(flush=True)`で出力する（`PYTHONUNBUFFERED=1`設定と合わせてリアルタイムに可視化される、3.4.33参照）。ERR-5008以外の通常時・他のAPIエラー時はこのログを出力せず、既存のログ量・頻度に変更はない。エラーハンドリング・リトライ挙動そのものは一切変更していない。

**切り分けの見方**: `sign_to_send_ms`が大きければ仮説(1)（署名〜送信遅延）を、`sign_to_send_ms`は小さいが送信した`api_timestamp`とレスポンスの`response_date`の差が許容範囲を超えていれば仮説(2)（GMO側許容窓・時刻差）を裏付ける材料になる。

**テスト**: `test_gmo_err5008_diag.py`（新規）で、ERR-5008時のみログ出力されること・診断ログの各フィールドが正しいこと・通常成功時や他のAPIエラー（ERR-5122等）時にはログが出ないこと・HTTPErrorとして返る経路でも同様に動作することを確認。関連19件パス。

**反映**: コミット`239b7c9`、VPS実機へ反映済み（建玉・activeOrders=0を確認後、engineサービスのみrebuild+restart、起動後WS接続・通常エントリーまで確認）。次回ERR-5008が発生するまで新しいログは出ない（9章参照）。

---

#### 3.4.51 孤児注文監視の週次メンテナンス時間帯スキップ（`scripts/check_orphan_orders.py`・`btc_trading_tool/virtual_trader.py`、2026-08-08実装、VPS未反映）

**背景**: 2026-08-08（土）09:00〜11:00の週次メンテナンス時間帯に、`orphan-orders-check`（5分間隔）から`[ALERT] wrapper exception / error=check_orphan_orders.py exit=1`が繰り返し届いた。`check_orphan_orders.py`は`trading_mode≠real`時のみAPI呼び出しをスキップする設計で、メンテナンス時間帯を考慮する分岐が存在しなかったため、メンテナンス中に応答しないGMO APIへ`fetch_active_orders()`を呼びに行き例外が発生していたと推定される。VPS本番稼働開始（8/1）後、通常運用中に迎える週次メンテナンスは8/8が実質初回だったため、これまで顕在化していなかった。実際の建玉・注文には影響なし（エンジン側の`_is_weekly_maintenance_window()`によるメンテナンス対応は別経路で正しく機能していた）。

**対応内容**:
- `virtual_trader.py`に、週次メンテナンス判定・週次プレメンテ判定・日次メンテナンス判定をモジュールレベル関数（`is_weekly_maintenance_window()`／`is_daily_maintenance_window()`／`is_weekly_pre_maintenance_window()`、およびこれらをまとめた`is_gmo_scheduled_maintenance_window()`）として切り出した。`VirtualTrader`の既存メソッド（`_is_weekly_maintenance_window()`等）はこれらを呼び出すだけの薄いラッパーに変更し、判定ロジック・時間帯の定義自体は変更していない
- `check_orphan_orders.py`の`check_orphan_orders()`で、`trading_mode≠real`によるスキップ判定の直後に`is_gmo_scheduled_maintenance_window()`によるスキップ判定を追加した。プレメンテ枠の分数は既存の`maintenance_prepare_minutes`（`config.json`、既定5分）をそのまま参照する。スキップ時は`trading_mode≠real`時と同様に心拍を更新し、GMO API・Telegramのいずれも呼ばない

**テスト**: `tests/test_check_orphan_orders.py`が21件全てパス（メンテナンス時間帯スキップ・プレメンテ枠スキップ・枠外での通常実行・`maintenance_prepare_minutes`反映・`VirtualTrader`側判定との一致、を新規追加）。

**未実施事項（v89で解消）**: VPS実機への反映は完了した（孤児注文監視側はhost側venvへ`git pull`、エンジン側はSHORT建玉を保持したまま`docker compose build`→`up -d --no-deps --force-recreate`でrecreate、integrity OK・WS再接続・SHA-256一致を確認）。あわせて週次メンテナンス判定の終了側猶予を11:10へ延長する追加修正（GMOの「プレ・オープン」仕様への対応）を行った。詳細はヘッダーv89変更履歴を参照。

#### 3.4.52 daily_history.jsonlの重複追記防止ガード（`scripts/build_daily_report.py`、2026-08-21実装）

**背景**: 2026-08-20 06:00直前のorder_rate_limit発動をきっかけに`manual_stop.flag`が作成され、直後の06:00`engine-restart`がSKIPされたことで、`trading_day_date`（3.4.8）が2026-08-19のまま約1日固定された（詳細は7.27）。この間、`append_daily_history()`は`trading_day_date`の重複チェックを持たず05:55のたびに無条件追記する実装だったため、8/20・8/21両朝の`daily_history.jsonl`に同一`trading_day=2026-08-19`の行が2回記録され、2行目は未リセットの`daily_realized_pnl`に翌日分の損益まで混入した誤値になっていた。

**対応内容**:
- `append_daily_history()`で、追記直前に`daily_history.jsonl`の最終行の`trading_day`と、これから追記しようとしている対象日を比較する
- 一致する場合は追記をスキップし、`duplicate trading_day detected...`のWARNINGログを出力する（根本原因である`trading_day_date`のロールオーバー遅延自体はこのガードでは解消しない。恒久対応は3.4.53参照）
- 一致しない場合は従来通り追記する

**データ修正**: 誤って記録されていた2026-08-19分の2行目（`realized_pnl≈-309.1`）を、バックアップ（`log/daily_history.jsonl.bak_20260821`）取得の上、`os.replace()`による安全な書き換えで削除した。1行目（`realized_pnl≈-86.4`、CSV取引日ウィンドウの実集計と一致）のみを正としている。

**テスト**: `tests/test_build_daily_report.py`に、最終行と同一`trading_day`での追記スキップ・異なる`trading_day`での通常追記の2ケースを追加（18 passed）。VPS実機へ反映済み（`daily-report`はhost側`.venv-host`実行のため`git pull`のみでrebuild不要）。

#### 3.4.53 `trading_day_date`の実時刻ベース日次ロールオーバー（`trading_engine.py`、2026-08-21実装）

**背景**: 3.4.8で述べた通り、`trading_day_date`はエンジン起動時の`initialize_daily_loss_state()`でしか更新されず、06:00固定の`engine-restart`が必ず動く前提に依存していた。2026-08-20、05:50:56のorder_rate_limit発動による`manual_stop.flag`作成が06:00の`engine-restart`をSKIPさせ、`trading_day_date`が約1日ずれたまま継続する事象が実際に発生した（7.27）。このずれは3.4.52の表示上の重複だけでなく、日次損失サーキットブレーカー（3.4.8、`daily_realized_pnl`ベースの判定）の基準日そのものにも影響しうる安全上の懸念であるため、恒久対応した。

**対応内容**:
- `trading_engine.py`に`_maybe_rollover_trading_day(trader)`ヘルパーを新設。`calc_trading_day_date()`と`trader.trading_day_date`を比較し、一致していれば何もしない（no-op、これが二重ロールオーバー防止のガードを兼ねる）
- 不一致の場合のみ、`trader._lock`下で`initialize_daily_loss_state()`相当の新サイクルへのリセット処理を実行する
- 通常メインループ（`sleep(1)`直後）・PAUSEDループ（`sleep(MANUAL_STOP_PAUSE_POLL_SEC=5)`直後）の両方から、`_write_live_state()`より前に呼び出す（`manual_stop`時はポジション強制決済される設計のため、ループ内でカウンタのみ実時刻ベースでリセットしても新たなリスクは生じないと判断）
- 06:00固定の`engine-restart.timer`はそのまま維持し、ロールオーバーはループ側を正としつつ冪等に共存する設計とした（restartが正常に動いた場合、新プロセスの起動時比較は同一日として no-op になる）

**テスト**: `tests/test_trading_engine.py`に追加（同一日内の複数回呼び出しでのno-op確認、日付跨ぎでの`trading_day_date`/`daily_start_balance`/`daily_realized_pnl`/勝敗カウントのリセット確認）。既存の`test_initialize_daily_loss_state_*`・`test_calc_trading_day_date_*`との矛盾なし。VPS実機へ反映済み（`docker compose build engine`→`up -d --force-recreate`、SHA-256一致確認済み、起動後エラーなし）。

#### 3.4.54 `ENTRY_COOLDOWN_AFTER_CANCEL_SEC`の5秒→10秒への延長（`btc_trading_tool/virtual_trader.py`、2026-08-21実装）

**背景**: 2026-08-20の相場ボラティリティ急変を受け、取引回数が平常時の約10倍（決済9.9倍・約定9.8倍）に急増し、`order_rate_limit`（1分5回超）が8/20〜21の2日間で31回発動した（すべて`recent_order_count=6`、深夜帯にも集中）。実データ調査の結果、AI夜間レビュー（5.4.2）によるエントリー閾値の緩和は無く（`imbalance_entry_threshold`等は据え置き、むしろサイズ縮小の防衛的変更のみ）、主因は相場急変そのものと、それに伴うimbalance反転キャンセル後の同一方向再エントリー（5〜10秒帯、当時の`ENTRY_COOLDOWN_AFTER_CANCEL_SEC`=5秒（3.4.35）をすり抜ける間隔）＋反対側への即時切替（`ENTRY_COOLDOWN_AFTER_IMBALANCE_CANCEL_ANY_SIDE_SEC`=1.5秒）の組み合わせと特定した。あわせて、2026-08-20 05:50:56のこのorder_rate_limit発動が`manual_stop.flag`を作成し、直後06:00の`engine-restart`をSKIPさせたことが3.4.53のインシデント（15時間停止・trading_day_date固着）の引き金になった可能性が高いと判断した（7.27）。

**対応内容**:
- `ENTRY_COOLDOWN_AFTER_CANCEL_SEC`を5秒から10秒に変更。実データによるシミュレーションで、対象期間に発火した31件のうち30件が回避可能と確認した上での変更
- `ENTRY_COOLDOWN_AFTER_IMBALANCE_CANCEL_ANY_SIDE_SEC`（1.5秒）・`ENTRY_COOLDOWN_AFTER_TIMEOUT_CANCEL_SEC`（300秒）は今回変更していない。反対側への即時切替（any-side）によるチャーンは今回のデータでは主要因ではないと判断したが、将来また同種の相場急変が起きた場合に単独で閾値へ到達するリスクは残るため、9章に低優先度の残課題として記録した
- 機会損失の概算: 10秒化でブロックされる約定群のPnLサンプルは-93円程度のネットマイナス寄りで、大きな機会損失の明確な証拠は見られなかった

**テスト**: 定数を参照する既存テスト（`pytest tests/test_virtual_trader.py -k "cooldown or cancel"`）17 passed。VPS実機へ反映済み（`docker compose build engine`→`up -d --force-recreate`、コンテナ内定数10確認・SHA-256/git blob一致確認済み）。

---

## 4. 運用インフラ（scripts/）


### 4.1 各スクリプトの役割

| スクリプト | 役割 | 二重起動対策 |
|---|---|---|
| `ensure_engine_running.ps1` | 手動でダッシュボードを開いた際、エンジンが未起動なら起動する | 全プロセススキャンで1件でもあればスキップ |
| `restart_engine.ps1` | 06:00に強制的に停止→起動し、新しい設定を反映する | ロックファイル（5分）＋全プロセス停止確認 |
| `engine_process_utils.ps1` | 上記2つが共通で使う関数群（`Find-AllEngineProcesses`等） | - |
| `process_launcher.py` | エンジンプロセスの起動＋ログの追記リダイレクトを担う、OS非依存の実体（Python）。`engine_process_utils.ps1`の`Start-EngineProcess`から呼ばれる | - |
| `detect_orphan_engines.ps1` | 1時間ごとに孤立プロセス・PID不一致を検知しログに残す | - |
| `check_live_state.py` | `live_state.db`の`updated_at`が直近10秒以内かを判定（環境変数`LIVE_DB`経由、`engine_process_utils.ps1`が設定） | - |
| `check_trading_anomaly.py` | 1時間毎の取引頻度・損益急変を検知しTelegram通知（実装済み、タスクスケジューラ登録済み、詳細は4.3・9章） | - |
| `check_engine_crash_loop.py` | Dockerコンテナ（engine/dashboard）がクラッシュループの末に停止していないかを監視しTelegram通知（実装済み、2026-07-14、詳細は4.5.6） | `runtime/crash_loop_notified_<service>.flag`による通知の重複防止 |
| `check_csv_db_consistency.py` | その日のCSVの決済件数とlive_state.dbのカウンター増分を突き合わせ、乖離していたらTelegram通知（実装済み、2026-07-14、詳細は3.4.9） | `runtime/check_csv_db_consistency.lock`（ロックファイル方式） |
| `check_monitor_heartbeats.py` | 上記3スクリプトが正常終了するたびに書き込む`runtime/monitor_heartbeats.json`を監視し、許容間隔（クラッシュループ検知は20分、それ以外は2時間）を超えて更新が無ければTelegram通知（実装済み、2026-07-15新規、詳細は9章） | `runtime/heartbeat_stale_notified_<script>.flag`による通知の重複防止 |
| `build_daily_report.py` | 毎日05:55（06:00の日次リセット直前）に、`trading_day_date`（06:00起点）を基準とした前日分の決済件数・勝率・実現損益をTelegramへ通知（実装済み、2026-07-16新規、詳細は9章）。決済件数・勝率は`realtime_trading_log_*.csv`の`reason`列（`TAKE_PROFIT`/`STOP_LOSS`/`FORCE_CLOSE_MAINTENANCE`）を対象期間（06:00〜翌06:00、暦日をまたぐため当日・翌日の2ファイルを参照）で集計し、実現損益は`live_state.db`の`daily_realized_pnl`をそのまま使用（`win_count`/`loss_count`は06:00以外の再起動でもリセットされるため使用しない）。2026-07-22、`append_daily_history()`に`total_assets_eod`（`portfolio_metrics.py`共通ロジックによる日末総資産）の記録を追加（詳細は4.6・7.24）。2026-08-21、同一`trading_day`への重複追記を防ぐガードを追加（詳細は3.4.52・7.27） | ロックは`run_daily_report.ps1`（PS1側）のみで管理。Python側（`build_daily_report.py`）に独自のロック機構は持たせていない（`check_csv_db_consistency.py`と同じ設計方針）。ログファイルへの書き込みも同様にPS1側のAdd-Contentのみに一本化（2026-07-17修正、詳細は9章。修正前はFileHandlerとの二重記録になっていた） |
| `build_public_report.py` | Xに公開してよい情報だけを抜き出した日次レポートを集計しXへ投稿（フェーズ1: テキストのみ、2026-07-22新規、詳細は4.6）。内部レポート（`build_daily_report.py`）はimportせず、独立モジュールとして許可リスト7項目のみを個別集計する | ロックなし（試験投稿期間中は手動実行のみのため。Task Scheduler登録時にPS1側でロック追加予定、詳細は9章） |
| `archive_old_logs.py` | 毎日04:30に、`log/`直下の日付付きファイル（`realtime_trading_log_*`・`market_snapshot_*`・AI議論ログ・監視ログ等、ファイル名に`YYYY-MM-DD`を含むもの）のうち、まるまる1ヶ月分が90日より古くなった月を対象に、月単位で`log/archive/log_{YYYY-MM}.zip`へ圧縮し元ファイルを削除（実装済み、2026-07-17新規、詳細は9章）。`config_history.jsonl`等の日付を含まない追記型ファイルは対象外。zip書き込み完了・`testzip()`による破損チェック通過を確認した後にのみ元ファイルを削除する順序を厳守。既存zipへの再開（前回実行が中断したケース）にも対応 | ロックは`run_archive_old_logs.ps1`（PS1側）のみで管理。Python側に独自のロック機構は持たせていない |

### 4.2 プロセス検出の設計方針（重要）
`trading_engine.pid`ファイルの中身だけに頼らず、`Get-CimInstance Win32_Process`で**コマンドラインに`trading_engine.py`を含む全プロセス**をスキャンする設計になっている（`Find-AllEngineProcesses`）。停止に1つでも失敗した場合、新規プロセスの起動には進まない。

`trading_engine.pid`自体は`trading_engine.py`が起動時に自分の`os.getpid()`を書き込み（`_write_pid()`）、停止時に削除する（`_remove_pid()`）。`process_launcher.py`や`.ps1`側はPIDファイルへの書き込みを行わない。

**プロセス起動処理のOS非依存化（2026-07-10）**: 以前は`Start-EngineProcess`内で`cmd.exe /c`向けにリダイレクト付きコマンド文字列を組み立てて`Start-Process -ArgumentList`に渡していたが、この方式に起因する事故（詳細は7.6）を受けて廃止した。現在はプロセス起動・ログの追記リダイレクトを`process_launcher.py`（Python、`subprocess.Popen`使用）に一本化しており、`.ps1`側は`python process_launcher.py`を呼ぶだけになっている。Windows/Linux共通のコードのため、将来のUbuntu VPS移行時もこのモジュールをそのまま流用できる想定。

### 4.3 登録済みタスクスケジューラ

| タスク名 | 実行時刻 | 内容 | MultipleInstances |
|---|---|---|---|
| `BTC_Engine_Restart` | 毎日06:00 | `scripts/restart_engine.ps1` | IgnoreNew |
| `BTC_AI_Nightly_Review` | 毎日03:30 | `ai_review/run_nightly_review.ps1` | IgnoreNew |
| `BTC_Detect_Orphan_Engines` | 毎時 | `scripts/detect_orphan_engines.ps1` | IgnoreNew |
| `BTC_Anomaly_Check` | 毎時（2026-07-12登録） | `scripts/run_anomaly_check.ps1`（内部で`check_trading_anomaly.py`を実行） | ロックファイル方式（`run_anomaly_check.lock`）で多重実行防止。7.8参照 |
| `BTC_Crash_Loop_Check` | 5分毎（2026-07-14登録） | `scripts/run_crash_loop_check.ps1`（内部で`check_engine_crash_loop.py`を実行、本番`docker-compose.yml`を監視） | `runtime/crash_loop_notified_<service>.flag`で通知の重複防止。詳細は4.5.6 |
| `BTC_CSV_Consistency_Check` | 毎時（2026-07-14登録） | `scripts/run_csv_consistency_check.ps1`（内部で`check_csv_db_consistency.py`を実行） | IgnoreNew。実機初回実行で正常動作を確認済み（詳細は3.4.9） |
| `BTC_Monitor_Heartbeat_Check` | 15分毎（2026-07-15登録、GUI登録） | `scripts/run_check_monitor_heartbeats.ps1`（内部で`check_monitor_heartbeats.py`を実行） | ロックファイル方式（`run_check_monitor_heartbeats.lock`）。実機初回実行でログ出力・正常終了を確認済み（詳細は9章） |
| `BTC_Engine_AutoStart_OnBoot` | Windows起動時（2分遅延、2026-07-15登録、GUI登録） | `scripts/ensure_engine_running.ps1`（PowerShell経由、`-ExecutionPolicy Bypass`） | 「ユーザーがログオンしているかどうかにかかわらず実行する」設定。`ensure_engine_running.ps1`自体の二重起動防止ロジック（全プロセススキャンで1件でもあればスキップ）に委ねる |
| `BTC_Daily_Report` | 毎日05:55（2026-07-17登録、GUI登録、既存タスクと同じ全般・条件・設定を踏襲） | `scripts/run_daily_report.ps1`（内部で`build_daily_report.py`を実行） | ロックファイル方式（PS1側の`run_daily_report.lock`のみ。実機初回実行でログ出力・Telegram通知を確認済み。詳細は9章） |
| `BTC_Log_Archive` | 毎日04:30（2026-07-17登録、GUI登録、既存タスクと同じ全般・条件・設定を踏襲） | `scripts/run_archive_old_logs.ps1`（内部で`archive_old_logs.py`を実行） | ロックファイル方式（PS1側の`archive_old_logs.lock`のみ。実機初回実行で対象月なし・正常終了を確認済み。詳細は9章） |
| `BTC_Public_Report_Post` | 毎日06:10（2026-07-23登録） | `scripts/run_public_report.ps1`（内部で`build_public_report.py`を実行） | ロック・当日取引日マーカー方式（`runtime/public_report_posted_<trading_day>.flag`で重複投稿防止）。ログオン設定は`BTC_Daily_Report`と同じくPassword方式。初回自動実行は2026-07-24予定、実機確認は未実施（詳細は4.6・9章） |
| `BTC_Engine_Process_Check` | 5分毎（2026-07-23登録） | `scripts/run_check_engine_process.ps1`（内部で`check_engine_process.py`を実行） | ネイティブ運用でのエンジン完全停止を検知し、`ensure_engine_running.ps1`による自動復旧を試みる（詳細は3.4.15）。当初1時間間隔の案だったが、検知の遅れを避けるため5分に短縮 |
| `BTC_SNS_Report_Post` | 毎日06:15（2026-07-23登録） | `scripts/run_sns_report.ps1`（内部で`build_sns_report.py`を実行） | ロック方式（重複配信防止マーカーは未実装。投稿コストが発生しないため実害が小さいと判断）。Instagram/Threadsへの実際の投稿は引き続き手動（詳細は4.7） |

### 4.4 Gitによるバージョン管理・デプロイフロー（2026-07-10導入）

**背景**: VPS本番運用開始後、ローカル（テスト・機能追加）とVPS（本番）でコードを分岐させず、ファイルの手動コピー＆ペーストによる人為的ミス（欠落・古い設定の上書き）を避けるため、Gitベースのデプロイフローに移行することとした。

**リポジトリ**
- ローカルフォルダ名・GitHubリポジトリ名ともに`gmo-btc-ws`（一致させ、`git clone`後にフォルダ名を変える手間をなくしている）
- GitHub: `https://github.com/festallypicco/gmo-btc-ws`（Private）

**Git管理対象外（`.gitignore`）**: 環境固有の実行時状態は一切コミットしない方針。
- `config/config.json` / `config/last_loaded_config.json`（AI議論パイプラインが毎晩自動書き換えするため、ローカル・VPSで内容が異なるべきファイル。誤って`git pull`で本番configがテスト環境の値に上書きされる、あるいはその逆を防ぐ）
- `live_state.db`、`log/`・`logs/`配下、`*.pid`、`manual_stop.flag`、`*.lock`
- `.env`系（APIキー。`*.env.example`のみ管理対象）
- `__pycache__/`等のPython生成物
- `back/`・`btc_trading_tool/back/`（過去コード・ログの一時退避場所、いずれ不要になる想定）

Git管理下に置くのは**コード（`.py`, `.ps1`, `.bat`, `.mdc`, `.md`）と`requirements.txt`のみ**。

**運用時のつまずきポイント（記録）**: 一度`git add`でステージ済みのファイルは、後から`.gitignore`に追記しても自動的には除外されない（`.gitignore`は未追跡ファイルにのみ効く）。`.gitignore`を後から整えた場合は、`git rm -r --cached .` でインデックスを一旦リセットしてから`git add .`し直す必要がある。稼働中のエンジンが書き換え続けているファイル（`live_state.db`等）が対象に含まれていると`git rm --cached`が拒否することがあるため、その場合は`-f`（強制、実ファイルは削除されない）を付けて実行する。

**想定デプロイフロー（VPS移行後に本格運用開始）**
```
# ローカル側（変更をpush）
git add .
git commit -m "変更内容"
git push

# VPS側（土曜09:00〜11:00のメンテナンス枠で実施）
git pull
# エンジン再起動（Ubuntu移行時のsystemd化は9章のTODO）
```

**ロールバック**
```bash
git log --oneline -5
git reset --hard <直前のコミットハッシュ>
```

**動作確認済みの範囲（2026-07-10）**: ローカルでのGit導入・初期化・GitHubへの初回push・別フォルダへのclone（pull相当の動作確認）まで完了。ローカル⇔VPS間での実際のpush/pullリハーサルは、VPS環境構築後に別途実施する。

**テスト配置の`tests/`サブディレクトリ運用への部分移行（2026-07-16頃〜）**: 1.1節のファイルツリーの通り、当初はテストをプロジェクトルート直下にフラットに置く方針だったが、2026-07-16以降に追加されたテスト（`test_csv_db_consistency.py`・`test_backtest_verifier.py`・`test_build_ai_review_summary_*.py`等）は`tests/`サブディレクトリに置かれる運用に自然と切り替わっていた。既存のルート直下テストを`tests/`へ移動する統一作業は行っておらず、2つの配置が併存する状態を許容している（無理に統一するより、既存テストの参照パス変更によるリスクを避けることを優先）。

**コミット滞留の解消（2026-07-19）**: 2026-07-10〜07-17にかけて実装された3.4.9〜3.4.12・4.5.6〜4.5.8関連の一連の変更（`tests/`配下の新規テスト・`deploy/systemd/`一式・各種監視スクリプトのLinux版等を含む）が、実装完了後もしばらく`git commit`されないまま滞留していたことが判明した。あわせて、ローカルには`origin/main`へ未`push`のコミットが5件残っていたことも判明した。原因は特定の事故ではなく、機能実装のたびに逐次コミットする習慣が徹底されていなかったことによる単純な滞留。2026-07-19に内容を確認の上、まとめてコミット・pushして解消した。**教訓**: 実装のたびに区切りの良いタイミングで`git commit`する（大きな塊になってから一括コミットしようとすると、差分の見直しに手間がかかり、未追跡ファイルの中に想定外のもの（今回の`tests/`ディレクトリ）が紛れていないかの確認負荷も上がる）。

### 4.5 Docker化（2026-07-11着手、検証中）

**背景**: ローカル（Windows11）とVPS（Ubuntu予定）でOS・実行環境が異なることによる不具合（パス区切り文字、文字コード等）を避けるため、Pythonの実行環境ごとコンテナに封じ込め、両環境で完全に同一のイメージを動かす方針とした（Gemini指摘の1番目）。優先度としては安全装置・デプロイフロー整備の後に位置づけていたが、着手済み。

**環境構築（2026-07-11）**: Windows11にDocker Desktopを導入。WSL2の「Linux用Windowsサブシステム」オプション機能が無効なままだとDocker Desktopのインストーラーがエラーを出さずに静かに失敗する事象を経験した。`Get-ChildItem -Path C:\ -Recurse -Filter "Docker Desktop.exe"`や`winget list`でも痕跡が見つからない場合、まずWindowsの機能（「Linux用Windowsサブシステム」「Virtual Machine Platform」）が有効化されているか（`wsl --status`）を確認するとよい。

**`Dockerfile` / `.dockerignore`の設計方針**: `.gitignore`（4.4節）と同じ考え方で、環境固有の実行時状態（`config/config.json`, `live_state.db`, `log/`配下, `.env`系等）はイメージに焼き込まず、コンテナ起動時にvolumeとしてマウントする。イメージにはコードと依存関係（`btc_trading_tool/requirements.txt`, `ai_review/requirements.txt`）のみを含める。ベースイメージは`python:3.13-slim`。

**構成方針**: エンジン（`trading_engine.py`）・ダッシュボード（`btc_trading_tool/dashboard.py`）をそれぞれ独立したコンテナで動かす。AI議論パイプライン等の定期実行は、当面は今まで通りホスト側のタスクスケジューラ（将来的にはVPSのcron）から`docker exec`または`docker compose run`で呼び出す方式とし、コンテナ内にcronを内蔵する完全な自己完結型オーケストレーションは今回のスコープに含めない（第一段階はシンプルさを優先）。

**検証済みの範囲**:
1. `docker build`によるイメージビルド成功、`pip list`で依存関係を確認
2. テスト用の隔離データ（`docker_test/`配下、本番の`config.json`/`live_state.db`とは別）を使い、単体コンテナでエンジンを起動。WebSocket接続・ログ出力（`market_snapshot_*.csv`, `config_history.jsonl`）・`Ctrl+C`（SIGINT）でのグレースフルシャットダウンを確認
3. `docker-compose.test.yml`により、エンジン・ダッシュボードの2コンテナを同時起動し、`config/`・`log/`・`live_state.db`をvolume経由で共有できることを確認（ダッシュボードが同一`config_version`を表示）

**Windows特有の落とし穴**: Dockerは、bind mount元のファイルがホスト側に存在しない場合、ファイルではなく**空のディレクトリ**として作成してしまう。`live_state.db`のような単一ファイルをマウントする際は、事前に`New-Item -ItemType File`で空ファイルを作っておく必要がある。

**設計課題と対応（2026-07-11に発見、2026-07-12に案Bとして解消）**: `manual_stop.flag`は「ファイルが存在する＝緊急停止」という存在ベースの判定のため、Windows特有の落とし穴（bind mount元のファイルがホストに存在しないと空ディレクトリとして作成されてしまう）と組み合わさると特に危険だった。対応案として案A（ルート全体マウント）・**案B（`runtime/`フォルダ新設、採用）**・案C（判定ロジック自体の変更）を検討し、案Bで実装した。

#### 4.5.1 案Bの実装内容（2026-07-12実施・完了）

**洗い出し**: 独自の監査スクリプト（`manual_stop.flag`・`live_state.db`・`trading_engine.pid`をキーワードとしたリテラル文字列＋定数名パターンの機械的検索）を作成し、プロジェクト全体を走査した。この過程で、当初の叩き台リストには含まれていなかった2つの見落としを発見した。

- `scripts/check_live_state.py`はパスをリテラルに組み立てず、環境変数`LIVE_DB`経由で受け取る設計だった（`engine_process_utils.ps1`が`$env:LIVE_DB`に実パスをセットし、渡している）。文字列検索では発見できないため、`LIVE_DB`というキーワードを追加して別途検索し、依存関係を確定させた
- `scripts/check_trading_anomaly.py`が`live_state.db`を直接参照する実装済みコードであることが判明した（詳細は7.8）。同ファイルが持つ`anomaly_state.json`（前回チェック時点の状態を保持する永続化ファイル）も、同じ「実行時状態ファイル」の性質を持つため案Bの対象に追加した

**実装**: 以下9ファイルのパス定数を`runtime/`配下を指すよう修正した（ロジック変更なし、`python -m py_compile`／PowerShellパーサーによる構文チェック済み）。

- `trading_engine.py`（3つ全て）、`btc_trading_tool/virtual_trader.py`、`btc_trading_tool/dashboard.py`、`scripts/reset_trading_state.py`、`scripts/check_trading_anomaly.py`（`live_state.db`・`anomaly_state.json`の両方）、`scripts/ensure_engine_running.ps1`、`scripts/restart_engine.ps1`、`scripts/detect_orphan_engines.ps1`、`scripts/engine_process_utils.ps1`
- `scripts/check_live_state.py`自体はコード変更不要（`engine_process_utils.ps1`側の`$liveDbPath`修正が環境変数経由で自動的に伝播するため）
- `.gitignore`・`.dockerignore`は、個別列挙していた`manual_stop.flag`・`live_state.db`・`live_state.db.bak*`・`scripts/anomaly_state.json`のエントリを`runtime/*` + `!runtime/.gitkeep`に一本化した（`*.pid`は`runtime/`以外の将来のPIDファイルも見込んで個別パターンのまま残した）

**既存状態ファイルの物理移動**: タスクスケジューラの自動再起動タスクを一時無効化→エンジンプロセスを直接停止（この時点でコードは既に新パスを参照するよう変更済みのため、`manual_stop.flag`経由の停止は新旧パスの不整合を招く可能性があり避けた）→`manual_stop.flag`（存在した場合）・`live_state.db`・`trading_engine.pid`・`scripts/anomaly_state.json`（存在した場合）を`runtime/`へ移動→エンジン起動→`runtime/live_state.db`の`updated_at`更新継続・`runtime/trading_engine.pid`の新規作成・ダッシュボードの緊急停止/再開ボタンによる`runtime/manual_stop.flag`の作成/削除、を確認済み→タスクスケジューラのタスクを再度有効化。

#### 4.5.2 docker-compose.test.ymlの修正（2026-07-12）

`live_state.db`単体のbind mountから`runtime/`フォルダ全体のマウントに変更した。この過程で、**修正前は`engine`・`dashboard`の2コンテナが別々のファイルシステム上に`manual_stop.flag`を持ちうる状態であり、ダッシュボードの緊急停止ボタンがengineコンテナに一切伝わらない可能性があった**ことが判明した（`live_state.db`のみ共有、他の状態ファイルは共有されていなかったため）。`runtime/`フォルダごと共有する方式への変更により、この問題も同時に解消された。

#### 4.5.3 本番用docker-compose.ymlの新規作成（2026-07-12、VPS未構築のため実機未検証）

案Aベースで作成されていた旧`docker-compose.yml`（ルート全体マウント、未使用のまま残置）を破棄し、`config/` `log/` `runtime/`の3フォルダのみをマウントする構成で新規作成した。

| サービス | 役割 | restart | 補足 |
|---|---|---|---|
| `engine` | `trading_engine.py` | `on-failure:5` | 非0終了時のみ再起動、最大5回。根拠は下記および4.5.6 |
| `dashboard` | Streamlitダッシュボード | `on-failure:5` | `127.0.0.1:8501`のみバインド（外部非公開） |
| `ai_review` | AI夜間レビュー | `"no"`（`profiles: [manual]`） | 常駐させず、systemdタイマーから`docker compose run --rm --profile manual ai_review`で都度起動 |

**`restart: on-failure`を選んだ根拠**: `trading_engine.py`の終了コードを洗い出したところ、`manual_stop.flag`検知による安全停止・SIGINT/SIGTERM受信はいずれも`sys.exit`を呼ばない自然終了（終了コード0）、`config.json`検証エラーおよび想定外の未捕捉例外は非0終了、という設計になっていることを確認した。これにより`on-failure`（非0終了時のみ再起動）を使っても、緊急停止直後にコンテナが再起動してしまう事故は起きない。

**最大リトライ回数`:5`について（2026-07-14追加）**: 当初は上限を指定しない`on-failure`のみだったが、`docker-compose.test.yml`での統合テスト中に「`config.json`が壊れた状態が続く限り無制限に再起動ループする」ことが判明したため、`on-failure:5`に変更した。上限到達後の検知・通知は4.5.6参照。

**環境変数の分離**: `GMO_API_KEY`・`GMO_API_SECRET`はこれまでdotenv経由のファイル読み込みがなく、Windowsのシステム環境変数に直接設定されていた（プロセス環境変数のみ参照）。Dockerコンテナはホストのシステム環境変数を自動継承しないため、新規にプロジェクトルート直下へ`.env`（実ファイル、Git管理対象外）を作成し、`engine`・`dashboard`サービスから`env_file`で渡す方式に変更した。`.env.example`（プレースホルダー版）はGit管理対象とした。`ai_review`サービスは既存の`ai_review/.env`をそのまま`env_file`で使用する。

**ダッシュボードの公開方式**: VPS上で直接ポート公開せず、SSHトンネル経由（`ssh -L 8501:localhost:8501 user@vps`）でアクセスする方針とした。個人利用のシステムであり、認証機構やTLS/リバースプロキシを別途構築するコストに見合わないと判断した。将来スマホ等からのアクセスが必要になった場合は、その時点でTailscale等のVPN方式を検討する。

#### 4.5.4 AI夜間レビューのVPS実行方式（2026-07-12、systemdタイマー採用）

ホストcron + `docker compose run --rm`案と比較検討した結果、**systemdタイマー**を採用した。理由は、`systemctl list-timers`で次回実行予定を確認でき、`journalctl`で過去の実行結果・exit codeを追跡できる点が、これまで大事にしてきた「ログをちゃんと残す」「見落としを防ぐ」という設計方針と合っているため（常駐コンテナ内でcronを動かす案は、コンテナ・依存関係管理が増える割にメリットが薄く不採用）。

`deploy/systemd/ai-review.service`（oneshot）・`deploy/systemd/ai-review.timer`（毎日03:30起動）をリポジトリ内にテンプレートとして作成した。`WorkingDirectory=/opt/gmo-btc-ws`は仮のVPS配置パスであり、実際のVPS構築時に調整が必要。

`ai_review/run_nightly_review.ps1`（Windows/PowerShell）はUbuntu上では動作しないため、同一ロジックのbash版`ai_review/run_nightly_review.sh`を新規作成した（ロック管理・`build_change_outcomes.py`失敗時は継続・`build_ai_review_summary.py`失敗時は`review_pipeline.py`をスキップ、という3つの分岐は完全に同一の挙動になるよう1文字も変えずに移植）。Windows版は削除せず、ローカルテスト環境では引き続き使用する。

#### 4.5.5 未検証の範囲（次回セッションの起点）

今回作成したのは全てVPS移行の「準備」であり、以下は実機での確認がまだ済んでいない。

1. ~~`Dockerfile`のビルドがLinux環境（Docker Desktop上のLinuxコンテナ、またはVPS実機）で実際に通るか~~ → 完了（9章参照）
2. ~~`run_nightly_review.sh`の構文チェック（Git Bash/WSL等での`bash -n`実行）~~ → 完了（9章参照）
3. ~~`docker-compose.test.yml`を使った統合テスト（`runtime/`共有方式の修正後、実際に`docker compose up`で起動確認していない）~~ → **完了（2026-07-14）**。engine/dashboard間の`runtime/live_state.db`・`config_version`共有、`manual_stop.flag`検知からの安全停止（保有ポジションのFORCE_CLOSE_MAINTENANCE経由フラット化込み）、`log/`共有をいずれも実機確認した。この過程で`restart: on-failure`の無制限リトライ問題を発見・対応（4.5.6参照）
4. ~~VPS実機の構築、`deploy/systemd/`配下のファイル配備と`WorkingDirectory`の実パスへの調整~~ → **完了（2026-08-01、ConoHa VPS）**。実機構築時に発生した多数のつまずき（SSH接続・鍵認証・Deploy Key・venv構築順序・Docker権限・GMO APIキー取り違え等）を反映し、`VPS構築手順書.md`をv11として全面刷新した（詳細は9章「VPS本番稼働開始」参照）
5. ~~SSHトンネル経由でのダッシュボードアクセスの実地確認~~ → **完了（2026-08-01）**
6. `docker-compose.test.yml`（テスト用compose）自体を`BTC_Crash_Loop_Check`の監視対象に含めるかどうか（現状は本番`docker-compose.yml`のみ監視。低優先度、9章参照）
7. ~~`engine`サービスの06:00強制再起動に相当する仕組みが未整備~~ → **完了（2026-07-14）**。詳細は4.5.7参照。VPS実機での`systemctl enable --now`実行は**完了（2026-08-01、9セット全て有効化済み）**

**ネイティブ→Docker本番切り替えの手順** → **完了（2026-08-01）**。①タスクスケジューラの無効化（`BTC_Engine_Restart`・`BTC_Detect_Orphan_Engines`・`BTC_Public_Report_Post`・`BTC_SNS_Report_Post`・`BTC_Engine_Process_Check`）→②ネイティブのエンジン・ダッシュボード停止→③real-test環境が起動していないことの確認→④Dockerコンテナ起動→⑤`docker ps`とネイティブプロセス一覧の両方で二重稼働していないことを確認→⑥systemdタイマー9セット有効化、の順で実施し完了した。無効化したタスクスケジューラは、VPS安定稼働確認までのロールバック手段として削除せず残す方針とした。

**引き続き案Bの範囲外（別途対応が必要、混同注意）**:
- ダッシュボードの認証追加は不要と判断（SSHトンネルで外部非公開のため）。将来VPN等での多人数アクセスに広げる場合は再検討
- `ai_review/.env`と`scripts/.env`が同じ`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`を別々に保持している重複は未解消（9章の低優先度課題）

### 4.5.6 クラッシュループ対策（2026-07-14実装）

**背景**: 4.5.5③の統合テスト中、`docker-compose.test.yml`の`config.json`を意図的に壊して`on-failure`の再起動挙動を確認したところ、**最大リトライ回数を指定していないと無制限に再起動ループし続ける**ことが判明した（7.12参照）。本番でも同種の障害（例: 書き込み中のディスク障害による`config.json`破損）が長時間続いた場合、ログが埋まり続ける・GMOへの接続試行を繰り返す等の懸念があるため対応した。

**対応方針**: 「一定回数失敗したら諦めて停止し、人間の介入を待つ」という、これまでの安全設計（3.4の安全装置群、6.7の緊急停止）と同じ考え方を踏襲した。無限リトライを断つだけでなく、**諦めて停止した状態を人間が気づけるようにする**ことをセットで実装した。

**1. リトライ上限（`docker-compose.yml` / `docker-compose.test.yml`）**
`engine`・`dashboard`両サービスの`restart`を`on-failure:5`に変更（4.5.3参照）。実測で5回まで再起動されることを確認済み。5回で回復しない場合はコンテナが`Exited`状態のまま停止し、それ以上の自動再起動は行われない。

**2. クラッシュループ検知（`scripts/check_engine_crash_loop.py`、新規）**
- `docker compose ps -a --format json`と`docker inspect`を組み合わせ、対象コンテナの状態（`exited`等）と`RestartCount`を取得する（`docker compose ps`単体には`RestartCount`が含まれないため）
- `--compose-file`/`-f`引数で監視対象のcomposeファイルを指定できる（本番用・テスト用のどちらにも使える汎用実装）
- いずれかのコンテナが`exited`状態かつ`RestartCount >= 5`の場合、「クラッシュループの末に停止した」と判定する
- 通知は`scripts/telegram_notifier.py`（既存の`send_telegram_message()`）をそのまま流用し、新規実装は行っていない（`ai_review/`側は無改変）
- **通知の重複防止**: `runtime/crash_loop_notified_<service>.flag`が存在しなければ通知を送りフラグを作成する。既に存在する場合は再通知しない（`manual_stop.flag`と同じ「存在ベース判定」パターンを踏襲）
- 対象コンテナが`running`に復帰したことを検知すると、対応するフラグを削除する（次にクラッシュループが起きた際に再通知できるようにするため）

**3. 定期実行**
Windowsタスクスケジューラに`BTC_Crash_Loop_Check`（5分間隔）を新規登録した。`BTC_Anomaly_Check`（4.3参照）と同じ登録方法を踏襲している。薄いラッパー`scripts/run_crash_loop_check.ps1`が本番`docker-compose.yml`を対象に`check_engine_crash_loop.py`を呼び出す。VPS移行後はsystemdタイマー化する想定（4.5.4のAI夜間レビューと同時期に対応）。

**動作確認（2026-07-14、`docker-compose.test.yml`にて）**: `config.json`を壊して5回リトライ後に`Exited (1)`で停止することを確認 → 手動実行でTelegram通知＋フラグ作成を確認 → 再実行で重複通知されないことを確認 → `config.json`復元・`RUNNING`復帰後にフラグが削除されることを確認。いずれも成功。

**4. VPS/Linux版の作成（2026-07-17実装、VPS未構築のため実機未検証）**
`scripts/run_crash_loop_check.sh`・`deploy/systemd/crash-loop-check.service`・
`deploy/systemd/crash-loop-check.timer`（5分毎）を新規作成した。
`restart_engine.sh`と同じ`-f`/`--compose-file`引数を持つ。`run_crash_loop_check.ps1`
自体にはロック・ログファイル・finally相当の仕組みが元々存在しないため、
ロックのみ`run_nightly_review.sh`と同じ経過時間ベース方式（ローカルGit Bashで
`flock`が使えないため）で新規に追加し、ログファイル出力は元のPS1に合わせて
追加しなかった。Telegram通知の条件・本文フォーマットは既存4本のラッパーと同一。
ローカルでの動作確認（`bash -n`・`docker-compose.test.yml`でのクラッシュループ
再現・通知確認）は完了。`systemd-analyze verify`はWSL経由で構文チェックのみ実施
（`docker.service not found`等の警告はWSL環境固有のノイズで実害なし）。
VPS実機での`systemctl enable --now`はVPS構築時に対応（4.5.7の
`engine-restart.timer`と同時期）。

**未対応（低優先度、9章参照）**: `docker-compose.test.yml`（テスト用compose）自体は`BTC_Crash_Loop_Check`の監視対象に含まれていない（本番`docker-compose.yml`のみ監視）。テスト用composeも監視したい場合は、別タスクとして登録するか、`run_crash_loop_check.ps1`にcomposeファイルを切り替える引数を追加する必要がある。

### 4.5.7 engineサービスの06:00定期再起動（`scripts/restart_engine.sh`、2026-07-14実装）

**背景**: ネイティブ環境ではTask Scheduler（`BTC_Engine_Restart`）が06:00に`scripts/restart_engine.ps1`を実行し、①AI夜間レビューによる`config.json`変更の反映、②3.4.8日次損失サーキットブレーカーの基準リセット、の両方を担っている。しかしVPS/Docker本番環境にはこれに相当する仕組みが存在しなかった（4.5.5で発見・記録した課題）。`ai-review.timer`（4.5.4）と同じ構成で解消した。

**`manual_stop.flag`が存在する場合の挙動**: ネイティブ版`restart_engine.ps1`は、`manual_stop.flag`が存在する間は「残存プロセスがあれば停止のみ行い、新規起動はスキップする」設計になっている（6章参照）。今回のLinux版でも「フラグが存在すれば再起動しない」という挙動を踏襲した。

ただし、ネイティブ版と異なり、Docker版では「停止」処理自体を明示的に行う必要がない。4.5.3節の通り`manual_stop.flag`検知によるエンジンの安全停止は`sys.exit`を呼ばない自然終了（終了コード0）であり、`restart: on-failure`（非0終了時のみ再起動）と組み合わさっているため、**`manual_stop.flag`でエンジンが停止した時点でコンテナは既に`Exited`状態になっており、Dockerが自動的に再起動することはない**。そのため`scripts/restart_engine.sh`は「フラグが存在すれば`docker compose restart engine`を実行しない」という判定のみで、ネイティブ版のような明示的な停止処理は不要となる。

**実装内容**:
- `scripts/restart_engine.sh`（新規）
  - `-f` / `--compose-file`引数でcomposeファイルを指定可能（デフォルト`docker-compose.yml`、`check_engine_crash_loop.py`の`--compose-file`と同じ考え方でローカルの`docker-compose.test.yml`でも動作確認できるようにした）
  - `runtime/manual_stop.flag`が存在する場合: `[SKIP] manual_stop.flag exists, restart skipped`をログ出力してexit 0（絵文字・特殊なUnicode文字なし）
  - 存在しない場合: `docker compose -f <compose-file> restart engine`を実行し、結果をログ出力する
  - `ai_review/run_nightly_review.sh`のようなロック管理は行っていない（systemdのoneshotサービスが多重起動しない前提のため）
- `deploy/systemd/engine-restart.service`（oneshot、`ai-review.service`と同じ形式。`WorkingDirectory=/opt/gmo-btc-ws`は仮のVPS配置パスで、実際のVPS構築時に調整が必要）
- `deploy/systemd/engine-restart.timer`（`OnCalendar=*-*-* 06:00:00`）

**動作確認（2026-07-14、`docker-compose.test.yml`にて）**: `bash -n`による構文チェックOK → `manual_stop.flag`なしで`scripts/restart_engine.sh -f docker-compose.test.yml`実行、engineコンテナの`StartedAt`が更新され再起動を確認 → `manual_stop.flag`ありで同様に実行、`StartedAt`が変化せず`[SKIP]`ログとexit 0を確認（テスト後はフラグを元の状態に戻し、`docker compose -f docker-compose.test.yml down`済み）。

**VPSでの有効化手順（参考、VPS未構築のため未実行）**:
```
sudo cp deploy/systemd/engine-restart.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now engine-restart.timer
```

**既存ファイルへの影響なし**: `scripts/restart_engine.ps1`・`ai_review/run_nightly_review.sh`・`deploy/systemd/ai-review.*`はいずれも無改変。

### 4.5.8 監視系3スクリプトのVPS/Linux版（2026-07-17実装、VPS未構築のため実機未検証）

**背景**: 4.5.6のクラッシュループ検知（Dockerコンテナ自体を監視）とは異なり、
`check_trading_anomaly.py`・`check_csv_db_consistency.py`・`check_monitor_heartbeats.py`
の3本はDockerコンテナの中身ではなく`runtime/live_state.db`・`log/`配下のCSV等を
ホスト側ファイルシステムから直接読む設計（bind mount経由でコンテナと共有される想定）。
そのためVPSホスト自体にPython3実行環境が必要になる点が4.5.6とは異なる（本節の対応は
スクリプト自体の移植のみで、VPSホスト側のPython環境構築はスコープ外）。

**移植元との違い（重要）**: 4.5.6で移植した`run_crash_loop_check.ps1`はロック・
ログファイル・finally相当の仕組みを元々持っていなかったため新規に設計を追加したが、
今回の3本（`run_anomaly_check.ps1`・`run_csv_consistency_check.ps1`・
`run_check_monitor_heartbeats.ps1`）はいずれも元々ロック・ログ・try/catch/finallyを
備えているため（詳細は4.3節）、「既存の機能を正確に踏襲する」移植とした。

**実装内容**:
- `scripts/run_anomaly_check.sh`・`scripts/run_csv_consistency_check.sh`・
  `scripts/run_check_monitor_heartbeats.sh`（いずれも新規）
  - ロック方式・ログ出力先・Telegram通知条件は対応する`.ps1`をそのまま踏襲した
  - **ロックファイルの配置場所**: `check_csv_db_consistency.py`用のみ`runtime/`配下
    （`run_csv_consistency_check.ps1`の既存仕様）、他2本は`scripts/`配下。この違いは
    そのまま踏襲し、統一はしていない
  - ロック内容の時刻文字列はPS1側`Get-Date -Format "o"`に対しbash側は
    `date -Iseconds`を使用（ロックの新旧判定自体はファイルの`mtime`ベースのため、
    この文字列表現の違いによる挙動差はない）
  - Git Bash向けに`cygpath -w`によるパス変換を行う（Linux VPSでは無変換、
    `run_crash_loop_check.sh`と同じ対応）
- `deploy/systemd/anomaly-check.service`/`.timer`（毎時）
- `deploy/systemd/csv-consistency-check.service`/`.timer`（毎時）
- `deploy/systemd/monitor-heartbeat-check.service`/`.timer`（15分毎）
  - いずれも`crash-loop-check.service`と同じoneshot形式。**`Requires=docker.service`は
    付けていない**（ホスト側ファイルシステムの監視のみでDockerに依存しないため、
    Dockerコンテナ自体を検査する`crash-loop-check.service`との意図的な差分）

**ローカルでの動作確認（2026-07-17）**: 3本×3パターン（正常終了／ロック中の多重実行
スキップ／Python異常終了時のTelegram通知）をすべて確認。`bash -n`で3本ともLF・構文
エラーなしを確認。`systemd-analyze verify`で6ファイルとも構文エラーなし
（NTFS由来の警告のみ、4.5.6の`crash-loop-check.*`確認時と同種でVPS実機では通常出ない）。

**依存ライブラリ**: 3スクリプトとも標準ライブラリと`telegram_notifier.py`（urllib使用）
のみに依存しており、`btc_trading_tool/requirements.txt`・`ai_review/requirements.txt`
への追加は不要と確認済み。

**読み取れなかった／曖昧だった挙動**: なし。3本とも移植元`.ps1`の構造がほぼ同型だった。

**VPSでの有効化手順（参考、VPS未構築のため未実行）**:
```
sudo cp deploy/systemd/anomaly-check.* deploy/systemd/csv-consistency-check.* deploy/systemd/monitor-heartbeat-check.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anomaly-check.timer
sudo systemctl enable --now csv-consistency-check.timer
sudo systemctl enable --now monitor-heartbeat-check.timer
```

**既存ファイルへの影響なし**: `scripts/run_anomaly_check.ps1`・
`scripts/run_csv_consistency_check.ps1`・`scripts/run_check_monitor_heartbeats.ps1`・
対応する3つの`.py`・`scripts/run_crash_loop_check.sh`・`ai_review/run_nightly_review.sh`・
`deploy/systemd/crash-loop-check.*`はいずれも無改変。

---

### 4.5.9 real mode隔離テスト環境（`docker-compose.real-test.yml`、2026-07-26新設）

実資金移行フェーズ2（隔離環境での実弾疎通確認、9章参照）用に、`config`/`runtime`/`log`を専用パスに分離した新しいcompose定義`docker-compose.real-test.yml`を追加した。既存のネイティブ運用・`docker-compose.test.yml`（仮想モードの隔離テスト用）とは完全に非干渉。

- real-test専用ダッシュボードを`http://127.0.0.1:8502`で追加（既存ダッシュボードとポート・状態ファイルを共有しない）
- `config.json`に`initial_jpy`を追加可能にし、`scripts/repair_live_state_equity.py`（3.4.14）と組み合わせて実際の入金額に内部帳簿を同期できるようにした
- `.gitignore`に`log-real-test/`・`runtime-real-test/`を追加
- **運用上の注意**: コンテナを`docker compose down`するとログが失われることを実地で確認した。以降、`down`実行前に`logs > file`へ退避してから停止する運用に変更した（原因調査中に07:09:42前後のエンジンクラッシュのログを喪失した事例あり、9章参照）

---

### 4.5.10 孤児注文監視のVPS/Linux版配備（2026-07-28実装、VPS未構築のため実機未検証）

4.5.8で整備した3スクリプト（`anomaly-check`・`csv-consistency-check`・`monitor-heartbeat-check`）に続き、`check_orphan_orders.py`（3.4.2拡張、3.4.25）のsystemd配備を追加した。詳細な修正内容は3.4.25参照。

- `deploy/systemd/orphan-orders-check.service`/`.timer`・`scripts/run_orphan_orders_check.sh`を新規追加（既存3セットと同形式、5分間隔）
- Windows側（`BTC_Orphan_Orders_Check`タスクスケジューラ登録、5分間隔）は無改変
- ローカルでの構文確認・import確認のみ実施。実機（VPS）での`systemctl enable --now`検証はVPS構築時に他5セットとまとめて実施する（4.5.8の「VPS実機の構築」参照）

```
sudo cp deploy/systemd/orphan-orders-check.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orphan-orders-check.timer
```

---

### 4.5.11 X投稿・SNS配信のVPS/Linux版配備（2026-07-30実装、VPS未構築のため実機未検証）

**背景**: 4.6・4.7で実装したX投稿（`build_public_report.py`）・SNS配信（`build_sns_report.py`）は、Windows側では`BTC_Public_Report_Post`（06:10）・`BTC_SNS_Report_Post`（06:15）としてTask Scheduler登録済みだったが、`deploy/systemd/`配下にはVPS/Linux版が存在しておらず、VPS本番稼働後にこの2つだけが動かなくなる見落としに繋がりかねない状態だった。

**対応内容**:
- `scripts/run_public_report.sh`・`scripts/run_sns_report.sh`を新規追加（既存の`run_orphan_orders_check.sh`等と同形式。ロック・ログ出力・失敗時のTelegramアラートを踏襲）
- `deploy/systemd/public-report.service`/`.timer`（毎日06:10）・`deploy/systemd/sns-report.service`/`.timer`（毎日06:15）を新規追加（`WorkingDirectory=/opt/gmo-btc-ws`の仮パス、oneshot形式は既存6セットと同じ）
- 改行コードはLFで新規作成し、`.gitattributes`の`*.service text eol=lf`・`*.timer text eol=lf`の対象に含めた
- Windows側（`BTC_Public_Report_Post`・`BTC_SNS_Report_Post`）は無改変
- ローカルでの構文確認・import確認のみ実施。実機（VPS）での`systemctl enable --now`検証はVPS構築時に他セットとまとめて実施する（4.5.8の「VPS実機の構築」参照）

**残課題**: ~~`run_public_report.sh`・`run_sns_report.sh`は`python`コマンドをそのまま呼び出しており、4.5.8で導入した3スクリプト向けのvenv（`.venv-host`）を明示的に参照する書き方にはなっていない~~ → **完了（2026-07-31、詳細は3.4.38）**。修正の過程で4.5.8の3スクリプトも実際にはvenvを参照していなかったことが判明し、5本まとめて統一した。

**投稿時刻の変更（2026-08-06決定・2026-08-07反映）**: 上記の06:10/06:15は登録当初の時刻。投稿直後の初速がその後の露出を左右するSNSアルゴリズムの特性を踏まえ、GMOメンテナンス時間帯（05:55〜06:30）と重なっておりSNS閲覧者もまだ少ない06:10/06:15から、朝の通勤・通学前でSNSを見る人が増える**07:30（`public-report.timer`）・07:35（`sns-report.timer`）**へ変更した。Xが先、SNSがその5分後という順序・間隔は維持。`OnCalendar`のみの変更で、投稿内容の生成ロジック（05:55の内部日次レポートのスナップショットを参照）には手を加えていない。VPS実機で`systemctl list-timers`により次回発火予定が07:30:00/07:35:00になっていることを確認済み。

```
sudo cp deploy/systemd/public-report.* /etc/systemd/system/
sudo cp deploy/systemd/sns-report.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now public-report.timer
sudo systemctl enable --now sns-report.timer
```

---

### 4.6 公開レポート自動投稿（X、フェーズ1、2026-07-22実装）

**目的**: 内部向け日次レポート（Telegram通知、4.1参照）とは別に、Xに公開してよい情報だけを抜き出した日次レポートを自動生成し、X（旧Twitter）へ自動投稿する。売買ロジックやパラメータの具体的な数値は公開せず、AIが売買ルールを決定し自動売買している旨と、結果指標のみを発信する方針（収益源となるロジック自体の模倣を防ぐため）。

**フェーズ分割**: 今回はフェーズ1（日次・テキストのみ）のみを実装した。画像（累積損益の推移グラフ等）添付、週次・月次レポートは別フェーズとして今後対応する（詳細は9章）。

#### 4.6.1 公開許可リスト

`build_public_report.py`は、以下9項目のみを`ALLOWED_KEYS`として定義し、出力直前に`collect_public_metrics()`がこのキーだけを持つdictへ詰め替え、`assert_only_allowed_keys()`で検証してから文字列化する。発注サイズ・imbalanceしきい値・クールダウン秒数等の非公開パラメータは、一時変数にも残らない設計。

**2026-07-22（v47）変更**: 当初は「累積収益率(%)」を含む7項目だったが、収益率は元本（初期資産）に依存するため、実資金移行時のリセットや将来の追加投入のたびに数字が不連続になる問題があった。これを避けるため、累積収益率(%)を廃止し、元本の増減に左右されない**累積損益(円)**・**当日損益(円)**・**当日増減率(%)**の3項目に置き換えた。

**2026-07-23（v49）変更**: 上記で追加した「当日増減率(%)」は、「当日損益(円)」と組み合わせることで前日の総資産（口座の実際の価値）を逆算できてしまうことが判明したため削除した（**絶対額の変化と、総資産に対する比率を同時に公開してはならない**という教訓。コード上にも再発防止のコメントを残している）。あわせて、勝率を`PUBLISHED_WIN_RATE`定数による累積/当日の選択式から、**累積勝率**・**当日勝率**の両方を常時出力する形に変更した（当日エントリー数が十分多く、統計的なブレの懸念が薄いため）。

| 項目 | キー | データソース |
|---|---|---|
| 累積損益(円) | `cumulative_pnl_jpy` | `live_state.db`の`cumulative_pnl`をそのまま円建てで使用（初期資産で割らない） |
| 当日損益(円) | `daily_pnl_jpy` | `daily_history.jsonl`の対象取引日の`realized_pnl`（`load_daily_pnl_from_history()`で取得）。**当初は`live_state.db`の`daily_realized_pnl`を使用していたが、これは06:00の日次リセットで0に戻るリアルタイムカウンタであり、06:10実行のこのレポートでは常に不正値になる設計ミスだったため2026-07-22に修正した。**該当日の行が`daily_history.jsonl`に無い場合は`RuntimeError`を送出し投稿を中断、Telegramアラートで通知する（不正確な値を投稿しないことを優先） |
| 決済件数 | `trade_count` | `realtime_trading_log_*.csv`を取引日窓（06:00〜翌06:00）で独立集計（旧キー名`settlement_count`から2026-07-23に変更） |
| 累積勝率(%) | `win_rate_cumulative_pct` | `live_state.db`ベースの累積集計 |
| 当日勝率(%) | `win_rate_daily_pct` | CSVベースの当日集計 |
| サーキットブレーカー発動有無 | `circuit_breaker_triggered` | `runtime/manual_stop_reason.json`の`triggered_at`が対象日窓内かどうか（真偽値のみ、理由・しきい値は読まない） |
| 稼働継続日数 | `uptime_days` | `daily_history.jsonl`の取引日数（旧キー名`operating_days`から2026-07-23に変更） |
| 当日エントリー回数 | `entry_count` | CSVの`reason=="ENTRY"`を独立集計 |
| 最大ドローダウン(%) | `max_drawdown_pct` | `daily_history.jsonl`の`realized_pnl`から資産曲線を再構成しピーク比で算出（確定損益ベース。含み損を含むドローダウンではない点に注意） |

**設計方針**: `build_daily_report.py`（内部レポート）はimportせず、独立モジュールとしてデータソースから個別に数値を取得する。内部レポートのコード変更が公開レポートに意図せず影響しない（またはその逆）ようにするため。ただし当日損益については、この独立取得の原則を保ちつつ、内部レポートが05:55に記録する`daily_history.jsonl`のスナップショットを参照することで、内部Telegramレポートの値と一致するようにしている（2026-07-22修正で確認済み）。4.7のSNS投稿用テキストも、この`collect_public_metrics()`の出力をそのまま再利用しており、許可リストの境界を二重管理しない設計になっている。

#### 4.6.2 X API連携

- `tweepy`（>=4.14.0、`scripts/requirements.txt`）を使用し、OAuth 1.0a User Context（API Key/Secret、Access Token/Secret）で認証する
- 認証情報は`X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET`として`.env`から読み込む（GMOコインAPIキーと同じ管理方式）。未設定時は`[ERROR]`を出して安全終了する（例外は投げない）
- 投稿は1回のみ。成功レスポンス（tweet id）を確認できたら再送しない。リトライは送信例外発生時のみ最大2回・間隔30秒とし、多重投稿を防止する
- 2026年以降のX APIは従量課金制（Pay-Per-Use、投稿1件あたり目安$0.01）。Xデベロッパーアカウント登録・$5のクレジット購入は2026-07-22に完了し、同日実機での初回投稿に成功した

#### 4.6.3 スケジューリングと運用状況（2026-07-24時点）

2026-07-22、Xデベロッパーアカウント登録・課金設定（$5クレジット購入）完了後、手動実行での初回投稿に成功（一時プロフィールページに反映されず「消えた」ように見えたが、キャッシュ反映遅延によるものと判明し、再読み込みで表示された）。2026-07-23、`BTC_Public_Report_Post`をTask Schedulerへ本登録（毎日06:10、ロック・当日取引日マーカーによる重複投稿防止、Password方式のログオン設定）。2026-07-24、初回自動実行を確認し、問題なく動作した。

#### 4.6.4 エラーハンドリング・テスト

- X投稿失敗時は既存のTelegramアラート機構（`send_telegram_message`、`Send-WrapperTelegramAlert`パターン）で即時通知する
- 当日損益の算出元である`daily_history.jsonl`に対象日の行が無い場合も同様に投稿を中断し、Telegramアラートで通知する（4.6.1参照）
- `--dry-run`モードで投稿内容を標準出力・ログに出すだけの確認ができる
- `tests/test_build_public_report.py`：許可リスト検証（`ALLOWED_KEYS`完全一致・非公開キーワード不在の正規チェック）、CSV集計、ドローダウン、サーキットブレーカー判定、認証未設定時の安全終了、ドライラン、当日損益の`daily_history.jsonl`フォールバック・欠落時のエラー通知を網羅。2026-07-22時点で195件パス

---

### 4.7 Instagram/Threads向けSNS投稿コンテンツ配信（ステップ1: テキスト+AI考察、2026-07-23実装）

**目的**: X（4.6）に加え、Instagram（リール＋キャプション）・Threads（テキスト中心）への展開を見据え、SNS投稿用のテキストコンテンツを自動生成し、専用のTelegram Bot経由で配信する。投稿自体（Instagram/Threadsへのアップロード）は引き続き人手によるオペレーションとする。動画（.mp4）生成はステップ2として別途対応し、今回は対象外。

**設計方針**: 4.6の許可リスト（`collect_public_metrics()`）をそのまま再利用し、内部データへ新たに直接アクセスする経路を作らない。これにより許可リストの境界を単一箇所で管理する。

#### 4.7.1 構成

- `scripts/build_sns_report.py`（新規）: 許可リスト済みデータの取得 → LLM考察の生成・結合 → SNS投稿用テキストの確定 → 専用Bot経由での配信
- LLM考察: Gemini/OpenAI（`.env`の`LLM_PROVIDER` / `LLM_API_KEY`で設定）に許可リスト済みの数値のみを渡し、1〜2文の一般的な所感を生成する。**プロンプトには「具体的な売買ロジック・パラメータ・設定変更には一切言及しない」ことを明示的に指示**しており、LLMが数値の変動理由を推測して発注ロジックの変更等を示唆する文章を生成しないよう制約している（非公開情報が考察文経由で漏れる、または誤った推測が事実であるかのように発信されることを防ぐため）
- LLM考察の生成に失敗した場合（レート制限等）は、考察文なしで基本項目のみのテキストを配信する。実際にGemini 429エラー発生時のフォールバック動作を実運用で確認済み
- **2026-07-24修正**: 実運用で「稼働4日目も着実に利益を積み」のように文が完結しない出力が確認された。原因はGemini 3.5の`thinking`が`max_output_tokens`を消費してしまい、実際の出力文が尻切れになっていたこと。プロンプトへの「句点で完結・体言止め禁止」の明示、few-shot例の追加、`max_output_tokens`拡大（2048）、`thinking_level=minimal`指定に加え、文末が句点で終わっていない考察文は`is_complete_commentary`で破棄しフォールバックする仕組みを追加した
- **免責文・ハッシュタグ（2026-08-04修正、v78）**: 免責文・ハッシュタグはLLMには生成させず、固定文言としてコード側で保持する（`INSTAGRAM_CAPTION_DISCLAIMER`・`INSTAGRAM_CAPTION_HASHTAGS`・`THREADS_TEXT_DISCLAIMER`）。当初はInstagram・Threadsとも仮想資金運用段階を前提とした免責文だったが、本番運用移行（`trading_mode=real`）に伴い、Instagram側は実資金運用である旨・投資助言でない旨・利益非保証を明記する文言に差し替え、Threads側にも同趣旨の免責文を新規に追加した。Instagramはアプリ側の表示仕様上ハッシュタグが5個までしか表示されないため、`INSTAGRAM_CAPTION_HASHTAGS`は5個に固定している。Threads免責文はAI考察生成用のプロンプト入力には含めない（`extract_threads_commentary()`で除外）。CTA文言・見出し・数値項目・冒頭フックは今回変更していない

#### 4.7.2 Telegram Botの分離

事故防止のため、内部管理用と公開用でTelegram Botを完全に分離した。

| Bot | 用途 | 環境変数 |
|---|---|---|
| 既存Bot | 内部管理用（`build_daily_report.py`、各種監視スクリプトのアラート） | 既存の`TELEGRAM_BOT_TOKEN`等 |
| 新規Bot（`SnsPostBot`） | SNS投稿用コンテンツ（テキスト、将来的に動画も）の配信 | `TELEGRAM_SNS_BOT_TOKEN` / `TELEGRAM_SNS_CHAT_ID` |

`scripts/telegram_notifier.py`・`ai_review/telegram_notifier.py`の両方に`send_sns_telegram_message()`を追加し、送信先を明確に分離。SNS用コンテンツ生成・配信に失敗した場合のアラートは、既存（内部管理用）Botへのみ送信し、SNS用Botへは送らない。

#### 4.7.3 内部日次レポートの拡張

`build_daily_report.py`（内部Bot）に、SNS投稿用テキストには含まれない管理用の情報を追加した。

- サーキットブレーカーの発動理由・しきい値との比較値（`runtime/manual_stop_reason.json`の詳細）
- 夜間AI議論の件数（提案件数・採用件数）
- ポジション復元イベントの有無（3.4.14で追加した起動時ポジション復元が対象日に発動したか。発動時は`log/position_restore_events.jsonl`に記録される）
- 監視系ハートビート状況（`runtime/monitor_heartbeats.json`の要約）
- `config_version` / `active_profile_name`
- （2026-07-25追加）前夜のAI議論の成否。失敗した場合は原因の種類（タイムアウト／混雑／クォータ超過／その他、5.4.1の`classify_llm_error_kind()`に対応）
- （2026-07-25追加）前夜のAI議論で設定変更が提案・適用された場合、変更されたパラメータ名と変更内容の概要

#### 4.7.4 運用状況（2026-07-24時点）

2026-07-23、`run_sns_report.ps1`（ロック機構付き）を新規作成し、`BTC_SNS_Report_Post`をTask Schedulerへ登録（毎日06:15、`BTC_Public_Report_Post`の06:10より後。Password方式のログオン設定）。Xと異なりTelegram配信自体はコストが発生しないため、実際の投稿判断（Instagram/Threadsへの手動投稿）は先送りしつつ、内容の自動配信は先行して本登録した（投稿コストがないため、Xの`public_report_posted_<trading_day>.flag`のような重複配信防止マーカーは今回は実装していない）。

2026-07-24、初回自動実行を確認。Telegram認証情報の設定漏れにより初回は失敗したが、設定後の再実行で成功した。

**テスト**: `tests/test_build_sns_report.py`等を新規追加。2026-07-24時点でプロジェクト全体で224件パス。

---

### 4.7.5 Instagramリール向け動画自動生成（ステップ2、2026-07-24実装）

**目的**: Instagramは「流し見」される前提のメディアであるため、動画冒頭（最初の1〜2秒）で「AIが自律的にBTCを売買している」という物語性を視覚的に伝えるフックを入れ、詳細な数字はキャプション側（4.7.1〜4.7.4で実装済みのテキスト）でロジカルに裏付ける、という役割分担で設計した。**この考え方はInstagramに限定したものであり、X・Threadsは引き続き数字中心の構成のまま変更していない**（読む文化・検索性・ロジカルな信頼感を重視する設計方針のため）。

#### 構成
- `scripts/sns_reel_video.py`（新規）: PILでフレーム画像を生成し、`imageio-ffmpeg`で動画に組み立てる。無音、1080×1920（9:16、Instagramリール向け縦型）、24fps、12秒
- **冒頭フック**: 固定テンプレート文を3パターン用意し、取引日でローテーション表示する。**LLMには生成させない**（煽り表現をLLM任せにすると、意図せず「他者への推奨」に近いニュアンスの文言が生成されるリスクがあるため、あらかじめ人手でレビュー済みの固定文言のみを使う）。3パターンとも3行構成で、中央の「BTC」だけ他の2行より大きく・太いフォントで強調表示する（1パターンは2026-07-24に3行構成へ先行改修済み、残り2パターンは2026-08-03に揃えて改修）。2026-08-03、本番運用移行（`trading_mode=real`）を受けて3パターンとも実資金運用である旨が伝わる文言に更新した: 「AIが実資金で自動売買／BTC／24時間稼働中」「AIが実資金で運用中／BTC／24時間自動売買」「実資金でAIが売買／BTC／休みなく稼働中」。あわせて、冒頭フックがテキスト静止のみだった点を改め、軽いフェードイン演出を追加した
- **指標表示**: フックの後、`collect_public_metrics()`（4.6.1の許可リスト9項目）を2枚のカードに分けてアニメーション表示する（損益・勝率系5項目／稼働状況系4項目）。動画独自のデータアクセスは行わず、テキストレポートと同じ関数の出力のみを使用
- **BGM**: 動画側では一切付与しない（Instagram投稿時に手動でBGMを追加する運用のため、音声トラック自体を持たない）
- **フォント**: 4.6.2で用意した同梱フォント（`assets/fonts/NotoSansJP-Regular.ttf`）に加え、数値表示用に等幅フォント（`assets/fonts/JetBrainsMono-Bold.ttf`、SIL OFLライセンス）を新規に同梱

#### 見た目デザイン（2026-07-24 改修、Geminiによる2回のレビューを反映）
初期実装（PILで文字を中央揃え・単色で1行ずつ表示）は「単調・素っ気ない」という指摘を受け、以下のダッシュボード風デザインに改修した。

- **レイアウト**: 項目名を左寄せ・数値を右寄せに配置するカード型レイアウト。指標を1画面ずつではなく複数行まとめたカード単位で表示し、画面切り替え回数を削減
- **フォントの強弱**: 項目名はNotoSansJP・やや小さめ・薄いグレー。数値はJetBrains Mono Bold・大きめで表示し、単位（円・%・日・件・回）のみ数字の75%サイズのNotoSansJPで分離描画する（「発動なし」等、数値＋単位の組み合わせでないテキスト値は対象外）
- **色分け**: 損益系の数値は符号に応じて色分け（プラス: 緑 `#64BEA3`系、マイナス: `#FF8A80`）。テキストレポート側と同じ符号ロジックを使用。マイナス側の色は当初やや暗く背景との視認性（コントラスト比）が緑より低かったため、`#FF8A80`に変更し是正済み
- **背景**: 純黒ではなく、わずかに青みがかったダークネイビー（RGB概ね(16,25,45)前後）
- **ヘッダー**: システム名（`BTC/JPY AUTO-TRADING SYSTEM`）と日付を表示。当初は指標カード画面にのみ表示され冒頭フック区間には表示されていなかったが、2026-08-03に動画の最初のフレームから最後まで常時表示される固定バーに変更し、冒頭フックの間も日付が見える状態にした。**「LIVE」「RUNNING」「SYSTEM STATUS: NORMAL」等、リアルタイムで実際に稼働・正常動作していることを断定する表示は引き続き意図的に含めていない**が、2026-08-03の本番運用移行（`trading_mode=real`）を受けて、ヘッダー付近に「実資金で稼働している」という事実のみを伝える短いバッジ表示を追加した（`trading_mode`の値を見て表示可否を切り替える設計。「絶対に儲かる」「元本保証」等の断定・勧誘的な表現は含めない）
- **画面遷移**: 画面全体の暗転（フェードアウト/イン）は使用せず、指標はフェードイン＋カウントアップで表示。カード間の切り替えはカット切り替えのまま（スライド等のトランジションは費用対効果が低いため見送り）
- **セーフゾーン**: Instagramアプリ側UI（上部のアカウント名表示・下部のキャプションやいいねボタン等）との重なりを避けるため、画面の上下に十分な余白を確保

#### 配信
- `send_sns_telegram_video()`（後述の`telegram_notifier.py`一本化後の実体）で、生成した動画とキャプションテキストをセットでSNS用Botへ配信する
- 動画生成に失敗した場合は、テキストのみ（4.7.1〜4.7.4のキャプション）でフォールバック配信し、内部Botへアラートする

#### QA用サンプルデータオプション（2026-07-24追加）
通常の`--dry-run`はその日の実データ（ログ）をそのまま使うため、当日損益がプラスの日にはマイナス側の配色を目視確認できない。QA専用に`--sample-data`フラグを追加し、当日損益をマイナスにしたテスト用の指標値で動画を生成できるようにした。テスト用データはコード上のモック値であり、実際のログファイルや本番データの参照・書き換えは行わない。フラグ未指定時の通常の`--dry-run`および本番の動画生成フローの動作には影響しない。

#### 動作確認（2026-07-24、`--dry-run` / `--sample-data`）
`log/sns_reel_2026-07-23_dryrun.mp4`（1080×1920、24fps、12.00秒、音声トラックなし）を生成し、内容を確認済み。見た目改修後、背景色・カード内テキストの座標・符号による色分け・カウントアップアニメーションを実測（ピクセル解析・OCR）で検証し、意図通りに動作していることを確認した。`--sample-data`で当日損益をマイナス（-86円）にしたサンプルでも生成・確認し、マイナス色（`#FF8A80`）の背景に対するコントラスト比が実測で約7.88（プラス側の緑とほぼ同水準）であることを確認した。冒頭フックの3行構成についても、「自律」の字がピクセルレベルで正しく描画されていることをあわせて確認済み。

**テスト**: フック固定・許可リスト境界・動画失敗時フォールバック・実ffmpegスモークテストを含め234件パス。見た目改修（単位フォント縮小・マイナス色変更）の追加テスト11件パス。フック3行化・`--sample-data`オプションの追加テスト分は今後反映（件数は次回のCursor実装報告を確認のうえ追記）。

---

### 4.7.6 `telegram_notifier.py`の一本化（2026-07-24実装）

**背景**: `scripts/telegram_notifier.py`と`ai_review/telegram_notifier.py`が並立しており、SNS用送信関数（`send_sns_telegram_message` / `send_sns_telegram_video`）を追加するたびに両方へ反映する必要があった（実際、4.7初期実装時に二重メンテナンスが発生していた）。将来のimport衝突・反映漏れを避けるため一本化した。

**実装内容**: 正規実装を`scripts/telegram_notifier.py`に集約し、`ai_review/telegram_notifier.py`は本体を読み込むだけの互換シムに置き換えた。どちらのパスから`import`しても同じ実体（`__file__`は`scripts/telegram_notifier.py`）を参照する。既存の`from telegram_notifier import ...`はそのまま動作する。230件パス。

---

### 4.7.7 Instagram/Threads自動投稿（フェーズA〜E、2026-08-06実装）

**目的**: 4.7.1〜4.7.6でTelegramへ配信していたSNS投稿用コンテンツ（キャプション・動画）について、そこから先の「Instagram/Threadsへの実投稿」を手動オペレーションからGraph API経由の自動投稿へ切り替える。承認ゲートは設けず、生成→Telegram配信のフローは投稿処理の成否に関わらず独立して継続し、失敗時は引き続きTelegram上のコンテンツから手動リカバリできる状態を維持する設計とした。

#### 構成（フェーズB・C: 投稿処理本体）

- `scripts/threads_poster.py`（新規）: Threadsテキスト投稿。`graph.threads.net`へのcreate container→publishの2段階。二重投稿防止は`runtime/threads_posted_<trading_day>.flag`
- `scripts/instagram_poster.py`（新規）: Instagram Reels動画投稿。`graph.instagram.com`（後述の通りドメインに注意）へのcreate container→ステータスポーリング（5秒間隔・最大60回≒5分）→publish→permalink取得。二重投稿防止は`runtime/instagram_posted_<trading_day>.flag`。コンテナ作成時に`thumb_offset`パラメータ（プロフィール一覧に表示されるサムネイルのフレーム位置、ミリ秒指定）を付与している（詳細は4.7.8）
- 両者とも、未設定・401/403等のトークン不備は`auth_error`、その他のAPIエラーは`api_error`に分類してTelegramアラート（内部管理用Bot）を送る。Instagram側は加えて「投稿対象の動画ファイルが見当たらない」場合を`api_error`と区別する独立ステータス`file_error`として分類する（アラート文面上でAPI障害と混同しないようにするため）
- `--dry-run`指定時は実際のファイル配置・API呼び出しを一切行わず、送信予定内容をログ出力・Telegram確認メッセージとして送るのみ（本番のキャプション・動画配信は行わない）
- 認証情報（`THREADS_ACCESS_TOKEN`/`THREADS_USER_ID`/`THREADS_USERNAME`、`INSTAGRAM_ACCESS_TOKEN`/`INSTAGRAM_USER_ID`）は`.env`から読み込む。Instagram側の読み込み処理は`threads_poster._merged_env()`を共用している

**Instagramのトークンとエンドポイントの対応関係（つまずきやすい点）**: Instagramのアクセストークンには取得経路によって2系統あり、呼び出すべきAPIドメインが異なる。`EAA`から始まるトークン（Facebookログイン経由）は`graph.facebook.com`宛、`IGAA`から始まるトークン（Instagramビジネスログイン経由、今回採用した方式）は`graph.instagram.com`宛が正しい。ドメインを取り違えると、トークン自体は有効であっても`Invalid OAuth access token - Cannot parse access token`という紛らわしいエラーになる。`instagram_poster.py`の`INSTAGRAM_API_BASE`は`https://graph.instagram.com/v26.0`に設定している。

#### 動画の公開経路（フェーズC: 一時公開用static配信）

Instagram Graph APIのReels投稿は、動画を「公開されたHTTPS URL」で渡す仕様のため、既存の「ダッシュボードは非公開（Tailscale経由のみ）」という方針とは別枠で、投稿用動画だけを一時的に公開する経路を新設した。

- 既存の独自ドメイン（`festallypicco.com`、メール用途で既存運用中）にランダムなサブドメイン（例: `m7x2kq9.festallypicco.com`）をAレコードでVPSのIPへ追加。既存のMX/SPF/DMARC（メール関連）レコードには影響しない
- VPS側はnginx＋Let's Encrypt（certbot、自動更新）で当該サブドメイン宛のみを受け付ける最小構成。配信ルートは`runtime/public_media/`に限定し、ホワイトリスト正規表現（`^[A-Za-z0-9]{32,}\.[A-Za-z0-9]{1,8}$`、32文字以上のランダムトークン＋拡張子）に一致するファイル名のみ配信、それ以外・ディレクトリ一覧・Host不一致はすべて404または接続切断
- **VPSプロバイダ（ConoHa）側のクラウドファイアウォール（セキュリティグループ）は、ufwとは別に、80/443番ポートをVPSのネットワークインターフェースへ明示的にアタッチしないと有効にならない点に注意**。ufwだけを開けても外部到達不可のままだった
- 同一VPS上でTailscaleが443番ポートを自身のTailscaleアドレス上で既に使用していたため、nginx側は443番の待受をワイルドカードではなく公開IPアドレスへ明示的に限定することで共存させた（既存のTailscale経由ダッシュボードアクセスへの影響なし）
- 投稿する動画は`secrets.token_hex(24)`（48文字）をファイル名にして`runtime/public_media/<token>.mp4`へ配置し、投稿成功時はその場で削除、失敗・タイムアウト時はリトライ用に温存した上で、フェイルセーフとして15分間隔のsystemdタイマーが作成から2時間経過したファイルを自動削除する
- 証明書透明性ログ（CT log）の仕組み上、このサブドメインの存在自体は外部から検索可能になる点は運用上把握しておく（ただしトークン付きパスは推測不能なため、動画本体への到達はできない）

#### `deliver_sns_report`の終了コード（`scripts/build_sns_report.py`）

Telegram配信とAPI自動投稿、それぞれの成否を切り分けて監視できるよう、3段階の終了コードとした。

| コード | 意味 |
|---|---|
| 0 | Telegram配信（Instagram・Threadsとも）成功、かつThreads・Instagram自動投稿もいずれもsuccess/dry_run/skipped |
| 1 | Telegram配信自体の失敗（最優先） |
| 2 | Telegram配信は成功したが、Threads・Instagramのいずれか（または両方）の自動投稿がauth_error/api_error/file_error |

Threads・Instagramの自動投稿処理は、生成〜Telegram配信のフローとは独立した箇所（前者はテキスト生成直後、後者は動画生成直後）で呼び出され、例外を投げても`api_error`として捕捉されるのみでTelegram配信を止めない。成功時はTelegram本文に自動投稿済みラベルと投稿URLを追記し、失敗時は内部管理用Botへ種別（認証/API/ファイル）を区別してアラートする。

#### BGM合成（`scripts/sns_reel_video.py`拡張）

4.7.5時点では音声トラックを持たない動画だったが、著作権上の懸念を避けるためGeminiで作成したオリジナル楽曲（`assets/audio/sns_bgm_01.mp3`）を動画生成の最終ステップとして合成するよう変更した。

- `mux_bgm_into_video()`を新設。映像ストリームは再エンコードなし（`-c:v copy`）、音声ストリームのみ新規エンコード（AAC）
- BGMが動画（12秒固定）より短い場合は`-stream_loop -1`でループし、長い場合は出力側の`-t`でトリムする、という同一のffmpegコマンドで両方のケースを処理
- 末尾0.5秒をフェードアウトし、`loudnorm`フィルタ（EBU R128準拠、I=-16 LUFS/TP=-1.5 dBTP/LRA=11 LU、1パス）で音量を正規化
- BGMファイルが存在しない・読み込みに失敗した場合は警告ログを出したうえで無音動画にフォールバックし、処理全体は失敗させない

#### トークン自動更新（フェーズD）

Threads・Instagramの長期アクセストークンはいずれも約60日で失効するため、`scripts/refresh_sns_tokens.py`と週次のsystemdタイマー（毎週月曜05:00 JST）を新設した。

- Threads: `GET https://graph.threads.net/v1.0/refresh_access_token`（`grant_type=th_refresh_token`）、Instagram: `GET https://graph.instagram.com/refresh_access_token`（`grant_type=ig_refresh_token`）でそれぞれ独立にリフレッシュし、一方の失敗がもう一方をブロックしない
- 成功時のみ`.env`の該当キーをアトミック書き込み（`os.replace()`）で更新し、他の行・他のキーには一切触れない。失敗時は`.env`の値を変更せず維持し、内部管理用Botへアラート（成功・失敗どちらも通知する）
- `--check-only`オプション: リフレッシュAPIを一切呼ばず、現在のトークンをMetaの`debug_token`で自己検証するだけの確認モード。ただし`debug_token`は本来アプリアクセストークン（`app_id|app_secret`）を要求する仕様のため、アプリの認証情報を`.env`に保持していない現状では有効期限の厳密な取得はできず、「確認不可」に安全にフォールバックする（リフレッシュAPIは呼ばれないため既存トークンへの副作用はない）。正確な残り日数を確認したい場合は、Meta App DashboardのApp ID/App Secretを`.env`に追加すれば対応可能（9章参照）

#### 運用上の注意点（テスト実行時の実API呼び出し）

`.env`に本番の認証情報を設定した状態でテストスイートを実行すると、`credentials=None`を明示的に渡しても内部の`get_threads_credentials()`/`get_instagram_credentials()`が`.env`の実認証情報にフォールバックしてしまい、テスト実行のたびに実際のThreads/Instagram APIが呼ばれる状態になっていたことが2回（Threads実装時・Instagram実装時それぞれ）判明した。実際にThreadsへテスト文言が投稿される事故が1件発生し手動削除、Instagram側はダミー動画のため公開には至らず未遂で済んだ。該当テストでは資格情報取得関数自体をテスト内で`None`に固定することで再発防止済み。**今後、新たに`_poster.py`系のスクリプトを追加する際は同種のテスト設計上の注意が必要**。

**テスト**: `tests/test_threads_poster.py`・`tests/test_instagram_poster.py`・`tests/test_refresh_sns_tokens.py`・関連する`test_build_sns_report.py`/`test_sns_reel_video.py`の更新を含め、SNS関連テスト全体で225件超パス（2026-08-06時点）。

#### 動作確認

`--dry-run`での送信予定内容確認後、実際にThreads・Instagram双方への本番投稿（2026-08-05分）に成功。二重投稿防止フラグにより同日分の再投稿は行われないことを確認済み。トークン自動更新タイマーも有効化済み（次回2026-08-10月曜05:00 JST）で、`--check-only`による実トークンでの動作確認も完了している。

### 4.7.8 Reelsプロフィール一覧サムネイルの改善（2026-08-07）

**背景**: v77（4.7.5）で冒頭フックに軽いフェードイン演出を追加したところ、Instagramのプロフィール画面の投稿一覧に表示されるサムネイルが、Instagram側のデフォルト仕様（`thumb_offset`未指定時は動画の先頭フレーム=0msを使用）により、フェードイン開始直後のほぼ文字が見えない真っ黒に近いフレームになってしまっていた（実際の自動投稿結果で発覚）。

**対応**: Instagram Reelsのコンテナ作成リクエスト（`media_type=REELS`）に`thumb_offset`パラメータ（サムネイルに使うフレームの位置、ミリ秒指定）を追加した。値はハードコードの固定値ではなく、冒頭フックのフェードイン演出に使っている定数（`TEXT_FADE_SEC=0.55`、3行のフェード開始をずらす`HOOK_LINE_STAGGER_SEC=0.18`）から、3行すべてが完全に不透明になる最初のフレーム（≈833ms）を算出し、そこに`HOOK_THUMB_OFFSET_MARGIN_MS=150`の余裕を加えた**983ms**を採用（フェード時間の定数を変更した場合、サムネイル位置も追従して自動的に再計算される）。動画本編（再生開始位置・フェードイン演出そのもの）には変更なし。

**確認**: フレーム0（従来）はフック文字がほぼ見えない状態、フレーム≈983ms（新サムネ）は3行とも白くはっきり見える状態であることを確認。単体テスト22件パス。VPS実機での`--dry-run`実行時のペイロードで`create_fields.thumb_offset="983"`を確認、Telegram確認メッセージにも`thumb_offset_ms=983`が表示されることを確認済み。コミット`f9603b0`、VPSへは`git pull`のみで反映（ホスト側venv実行、Docker再ビルド不要）。

---

## 5. AI議論パイプライン（ai_review/）

### 5.1 目的
毎晩03:30に前日までのログを分析し、AI同士の議論を経て`config.json`の時間帯プロファイルを見直す。
06:00の`BTC_Engine_Restart`で変更が反映されるまで、約2.5時間の余裕を確保している。

**変更履歴**: 当初00:30実行だったが、Gemini API 503エラーの多発
（米国太平洋時間の業務ピーク帯と重なっていたと推測される）を受けて
03:30に変更。詳細は5.4.1参照。

### 5.2 データ集計（build_ai_review_summary.py）
4つの期間ウィンドウで集計する。

| ウィンドウ | 期間 | 用途 |
|---|---|---|
| `anomaly_check` | 前日1日 | 異常検知専用。単体で判断材料にしない |
| `rule_review` | 直近14日 | メインの判断材料 |
| `stability_check` | 直近30日 | 週次内訳で安定性確認（`weekly_breakdown`はProposerには含めず集計値のみ、Skeptic/Moderatorには週次内訳も。2026-07-15、7.7再発対応） |
| `regime_reference` | 直近90日 | レジーム参照（Proposerには集計値のみ、Skeptic/Moderatorには日次生データも） |

各ウィンドウ・プロファイル区分ごとに`confidence`（`insufficient`/`low`/`medium`/`high`）をサンプル数・実データ日数から自動算出する。データが要求日数分揃っていなくてもエラーにせず、実際に存在する日数だけで集計する。

### 5.2.1 約定データの集計（market_trades、2026-07-17実装）

`rule_review`（14日）・`stability_check`（30日）・`regime_reference`（90日）の各ウィンドウに、`market_snapshot`の`trade_count`/`buy_volume`/`sell_volume`（3.3.1参照）を集計した`market_trades`ブロックを追加した。`overall`（ウィンドウ全体）と`per_profile`（各プロファイルの`start_time`〜`end_time`でフィルタした内訳）の両方を持つ。

集計項目: 買い成行出来高合計・売り成行出来高合計・買い比率（分母0の場合は`null`）・snapshot1件あたりの平均約定件数・trades列が実際に存在した日数（`trades_actual_days`）。

**旧ログ（2026-07-17より前、trades列自体が存在しない日）の扱い**: `load_market_rows`が日単位でtrades列の有無を判定し、無い日は`has_trades_data=False`として集計から除外する（0扱いにしない）。全日が旧ログの場合、比率等はすべて`null`になる。

**信頼度ラベル**: `trades_actual_days`をもとに、既存の`confidence`と同じ閾値ロジックで`trades_confidence`という独立したラベルを付与する。取引ログのサンプル数に基づく既存の`confidence`自体は変更していない。

### 5.2.2 板の厚みの集計（market_depth、2026-07-17実装）

`market_trades`（5.2.1）と同じ設計で、`market_snapshot`の`bid_depth5_size`/`ask_depth5_size`/`depth_imbalance`（3.3.2参照）を集計した`market_depth`ブロックを14日/30日/90日の各ウィンドウに追加した。

集計項目: `bid_depth5_size`の平均・`ask_depth5_size`の平均・`depth_imbalance`の平均（`depth_imbalance`が`null`の行は平均計算から除外）・depth5列が実際に存在した日数（`depth_actual_days`）。

**旧ログの扱い**: `load_market_rows`が`has_depth_data`/`depth_days`でdepth5列の有無を日単位判定し、無い日は0扱いせず集計から除外する（`market_trades`の`has_trades_data`と同じ考え方）。

**信頼度ラベル**: `depth_actual_days`をもとに`depth_confidence`という独立したラベルを付与する。既存の`confidence`・`trades_confidence`は変更していない。

### 5.2.3 ボラティリティの集計（market_volatility、2026-07-17実装）

`market_trades`（5.2.1）・`market_depth`（5.2.2）と同じ設計で、`market_snapshot`の`volatility_5min_range_pct`（3.3.3参照）を集計した`market_volatility`ブロックを14日/30日/90日の各ウィンドウに追加した。

集計項目: `volatility_5min_range_pct`の平均・最大値（急変動を見逃さないための参考値）・列が実際に存在した日数（`volatility_actual_days`）。

**旧ログ・null行の扱い**: `load_market_rows`が`has_volatility_data`/`volatility_days`で列自体の有無を日単位判定し、無い日は0扱いせず集計から除外する。列は存在するが値が`null`の行（エンジン起動直後のバッファ不足行）も、平均・最大値の計算からその行だけ除外する（0扱いにしない）。

**信頼度ラベル**: `volatility_actual_days`をもとに`volatility_confidence`という独立したラベルを付与する。既存の`confidence`・`trades_confidence`・`depth_confidence`は変更していない。

### 5.2.4 設定変更の事後評価（`ai_review/build_change_outcomes.py`）

`config_history.jsonl`に記録された各設定変更について、変更前(before)・変更後(after)の一定期間の取引成績（勝率・実現損益等）を比較し、`change_outcomes.jsonl`へ記録する。記録された内容は`prompts.py`経由でAIへのプロンプトに含まれ、「過去の変更が実際に効果があったか」をAI自身が次回提案の参考にできるようにする位置づけ。

**before側のconfig_version厳密化（2026-07-19修正、詳細は7.22参照）**: `_select_rows_for_window()`は、変更後(after)の取引データを`config_version == after_version`で厳密に絞り込む一方、変更前(before)の取引データは時間範囲のみで絞り込んでおり、`config_version`による一致条件が欠けていた。after側と同じ基準（`config_version == before_version`）に揃えるよう修正した。5.4.2のpending_rollout中間ステップのように短期間で`config_version`が更新され続けるパラメータでは、この修正により`before`側のサンプル数が減り`insufficient`判定が増えるが、これは意図した挙動である。

### 5.3 議論の3役

| 役割 | モデル | 入力 |
|---|---|---|
| Proposer（提案役） | Groq `openai/gpt-oss-120b` | 軽量版（`regime_reference`・`stability_check.weekly_breakdown`は集計ブロックのみ、`recent_config_changes`は直近3件・`reason`は約300文字に切り詰め。7.7参照） |
| Skeptic（懐疑役） | Gemini `gemini-3.5-flash` | フル版（90日の日次データ込み） |
| Moderator（調停役） | Gemini `gemini-3.5-flash` | フル版。必ずJSONのみを出力 |

異なるモデル系統（Groq/Gemini）を使うことで、片方の推論の癖・過学習をもう片方がチェックできる設計。

`market_trades`（5.2.1）・`market_depth`（5.2.2）・`market_volatility`（5.2.3）のブロックは3役とも集計値のみを渡す。`regime_reference`の日次生データ（Skeptic/Moderatorのみに渡す既存の仕組み）とは異なり、これらは日次の生データを一切渡さない（プロンプト肥大化対策、7.7参照）。Skepticには`depth_confidence`・`volatility_confidence`の確認項目も追加した。Proposer向けログに`market_trades_delta`・`depth_delta`・`volatility_delta`（各集計ブロック追加によるプロンプト文字数の増分）を出力し、肥大化を継続的に監視できるようにしている。Moderatorの出力スキーマ・バリデーションはSummary JSONにブロックが追加される以外に変更はない。

### 5.4 安全装置（ガードレール、多段構成）

1. **Moderator出力のリトライ（最大3回）**: JSONパース失敗・`validate_profiles()`失敗時は、エラー内容をMod eratorに渡して再生成させる。3回失敗した場合は**その夜の変更を見送り、`config.json`は変更しない**（`log/ai_validation_failures.jsonl`に記録、正常終了扱い）
2. **プロファイル名セット一致チェック**: 現行の`config.json`とプロファイル名の集合が完全一致しない場合（追加・削除・改名）、自動適用せず保留（`decision_doc.status = "held_profile_name_mismatch"`）。人間が内容を確認し、妥当なら手動で反映する
3. **変更幅の相対上限（±15%）**: 1晩あたりのパラメータ変更を前回比±15%に制限。超えた項目は前回値に据え置く
4. **絶対範囲チェック**: `config_manager.py`の`PARAMETER_ABSOLUTE_BOUNDS`で、各パラメータの絶対的な上下限を機械的に強制する（初期案であり運用しながら見直す前提）。`max_order_size_btc`（0.01〜0.20 BTC）もこのチェック対象に含まれる
5. **過去の失敗の記憶**: `ai_validation_failures.jsonl`の直近失敗をAIへのプロンプトに含め、同じ間違いを繰り返しにくくする
6. **アトミックな書き込み**: `config.json`は一時ファイル→`os.replace()`で書き込み、途中でプロセスが落ちても壊れない
7. **発注サイズの上限（`max_order_size_btc`）**: 板の厚み（0.01〜0.05 BTC程度）やエントリー条件（`min_entry_wall_btc`等）との整合性から、1回の発注サイズを0.05 BTCに制限する。現状は固定0.001 BTCのため表面化しないが、将来の動的サイジング再導入に備えた基盤として`virtual_trader.py`側で実効的にクランプされる
8. **その日の発注サイズ上限（`daily_target_order_size_btc`、任意・プロファイル単位）**:
   AIナイトリー議論が「その日はより小さいサイズに抑えた方が良い」と判断した場合に、
   0.001〜0.05 BTCの範囲で提案できる（`PARAMETER_ABSOLUTE_BOUNDS`）。範囲外の値は
   `safe_update_config()`が拒否し既存値（null含む）を維持、拒否理由は
   `updated_reason`・`decision_doc["rejected_daily_target_order_size_reasons"]`に記録。
   実際の発注サイズは以下の3値の最小値で決まり、**このパラメータは既存の
   `max_order_size_btc`（0.05固定）・資産20%ルールをどちらも緩める方向には
   働かない**:
   ```
   effective_size = min(
       computed_size,          # 資産20%ルール等から算出される希望サイズ
       max_order_size_btc,     # 0.05固定、AI変更不可
       daily_target_order_size_btc  # 任意。未設定(null)なら候補から除外
   )
   ```
   未設定（null）時は従来と完全に同一の挙動になることをテストで確認済み
   （`test_daily_target_order_size_btc.py`）。値・理由（`daily_target_order_size_reasoning`）
   は共にプロファイル単位で管理される。反映タイミングは他のプロファイル
   パラメータと同じく、翌06:00の`BTC_Engine_Restart`。
9. **外れ値検知（`judge_outlier()`）**: 過去の傾向から外れた変更提案を統計的に検知する（詳細は5.4.2）
10. **段階適用／カナリア方式（`pending_rollouts`）**: 外れ値と判定された変更のみ、3日かけて段階的に反映する（詳細は5.4.2）
11. **バックテスト検証ゲート（`ai_review/backtest_verifier.py`、2026-07-17実装）**: `imbalance_entry_threshold`/`take_profit_pct`/`stop_loss_pct`の変更提案を過去データで簡易シミュレーションし、悪化と判定されたら適用しない（詳細は5.4.3）

### 5.4.1 LLM通信エラー対応（`ai_review/llm_clients.py`、実装済み）
2026-07-08 0:30実行時に複数のAPI関連エラーが発生したため対応した。

| 問題 | 対応 |
|---|---|
| Proposer（Groq）の出力が`max_tokens=1000`で途中切れ | `max_tokens=3000`に変更。Groq TPM上限8000に対し入力約4488トークン＋出力枠を踏まえた値 |
| 3000トークンでもなお途中切れするケース | 根本対策としてProposerプロンプトを簡潔化（各提案1〜2行、長文の期待効果・注意点は不要、全体約1500トークン以内、絵文字・装飾記号禁止） |
| Gemini（Skeptic）が60秒でタイムアウト | リクエストタイムアウトを120秒に延長。`_is_timeout_error()`を追加し、一過性のタイムアウトもリトライ対象に |
| 5xx（一時的サーバー障害）がリトライされていなかった | `_is_transient_server_error()`を新規追加（500/502/503/504・"unavailable"等の文言を判定）。429判定と統合した`_is_retryable_error()`に切り替え |
| Geminiの日次クォータ超過（`RESOURCE_EXHAUSTED` + `RequestsPerDay`系）も他の429と同様に最大5回リトライしてしまい、回復しないエラーに残りクォータを浪費していた | `_is_daily_quota_exceeded_error()`を新規追加し、`_is_retryable_error()`とは独立してリトライループの最初にチェック。該当時はリトライせず即座に失敗させる（ログ: `[LLM] daily quota exceeded. no retry.`） |
| （2026-07-25）夜間の自動実行1回に加え手動再実行を重ねたところ、タイムアウト2回・503（混雑）1回・429（日次クォータ超過）1回と連続で失敗し、深夜に気付いた時点で対応しようがなかった | エラー種別判定を`classify_llm_error_kind()`（タイムアウト／混雑／クォータ超過／その他）に統一。タイムアウト・混雑（503等の一時障害）は30秒間隔で最大3回まで再試行し、クォータ超過は引き続き即座に失敗させる。Proposer（Groq）のタイムアウトも同じ再試行対象に含めた |

**設計上の注意**: 日次クォータ判定は文字列の部分一致（`resource_exhausted`かつ`requestsperday`/`perday`を含むか）で行っているため、分単位のレート制限（`RequestsPerMinute`）と誤判定しないことをダミー文字列で確認済み。

**実行時刻の見直し（2026-07-08）**: 0:30実行時の503エラー多発・リトライ
上限到達によるAI議論スキップが発生したため、実行時刻を00:30から03:30に
変更した。この時刻は、第三者の観測データに基づく太平洋時間の混雑ピーク帯
（JST換算で概ね01:00-03:00・05:00-07:00）の谷間にあたる。ただし
Google公式の傾向データではなく、あくまで参考情報に基づく調整である点に
留意。今後も503発生率を継続的に観察し、必要なら再調整する。

**Proposer（Groq）のTPM超過対応（2026-07-10）**: `reduced_summary`が
`past_validation_failures`のバリデーション失敗時の生JSON出力全文
（`last_output`）を含んだまま肥大化し、Groq TPM上限8000を超えて
413エラーが発生した（詳細は7.7）。対応として、Proposer向けの
`reduced_summary`構築時に`last_output`を約300文字に切り詰め、
`call_groq()`の`max_tokens`を3000から2000に変更した。Skeptic/Moderator
（Gemini）向けのプロンプトは変更していない。なお、この413エラーは
Gemini File API/Media APIとは無関係（GroqのTPM制限であり、Geminiの
ファイルサイズ制限ではない）。誤って混同しないよう記録しておく。

### 5.4.2 外れ値検知＋段階適用（カナリア方式、2026-07-10実装）

**背景**: AI議論は03:30開始・06:00反映という深夜〜早朝サイクルで完結する設計のため、この間に人間の事前承認を挟むと運用のボトルネックになる（早朝の手動承認は現実的でない上、平常運転が続くと承認が形骸化し「機械的に押すだけ」になるリスクが高い）。そのため、人間の事前承認の代わりに、統計的な異常検知＋段階適用によって「いつもと違う提案」だけを自動的に足止めする設計とした。

**外れ値判定（`judge_outlier()`、`review_pipeline.py`）**
Moderator承認後・config書き込み前に、変更のあった各パラメータについて以下いずれかに該当すれば外れ値と判定する（OR条件）。

| 条件 | 内容 |
|---|---|
| Zスコア | 直近14日間（`update_log.jsonl`の実績反映値ベース）の変更幅の平均・標準偏差から算出したZスコアの絶対値が`2.0`超 |
| データ不足 | 直近14日間の有効データが5件未満（新パラメータ導入時等） |
| 逆方向 | 直前の適用方向と今回の提案方向が逆（往復動作の抑制） |
| 累積乖離 | 直近3日間の高値・安値の乖離幅が20%超（単日の±15%キャップ内でも検知） |

統計データは**AIの生の提案値ではなく、実際にconfigへ反映された実績値**を使う（保守的な設計方針。AIが強気な提案を連発しても検知基準自体が押し流されないようにするため）。

**段階適用（`pending_rollouts`、`config.json`）**
外れ値と判定された変更のみ、`[30%, 60%, 100%]`・3日固定のスケジュールで段階的に反映する。

```json
"pending_rollouts": {
  "profiles.night.imbalance_entry_threshold": {
    "start_value": 0.58, "target_value": 0.66,
    "current_applied_value": 0.606, "start_date": "2026-07-09",
    "day_index": 1, "total_days": 3, "rollout_ratios": [0.3, 0.6, 1.0],
    "direction": "up", "reason": "zscore=2.4", "status": "in_progress"
  }
}
```

- 毎晩の処理冒頭で`apply_daily_rollouts()`が既存の`pending_rollouts`の当日分を先に適用してから、その夜の新規提案の処理に進む
- 最終日（3日目）は端数調整のため`target_value`を直接採用し、丸め誤差を残さない
- **翌日の新規提案は常にその時点のconfig実値を起点にする**。前日の未消化分（段階適用の途中経過）は持ち越さず、`create_or_replace_pending_rollout()`が都度作り直す
- 変更幅が`PARAMETER_MIN_STEP`（パラメータごとの最小ステップ幅、`config_manager.py`）未満の場合は段階適用をスキップし即時フル反映する
- 既存の±15%相対キャップ・`PARAMETER_ABSOLUTE_BOUNDS`は、段階適用中の値にもそのまま`safe_update_config()`経由で適用され続ける（変更されていない）

**Telegram通知の位置づけ変更**: 「承認してください」から「本日の変更内容をお知らせします」という事後報告に変更。外れ値判定があった夜は該当パラメータと判定理由（`reason`）を通知内で強調表示する。承認ボタン等の対話的要素は追加していない。

**revert時のメタデータ食い違い（2026-07-15解消）**: 理論上、AIの生の提案値が非常に大きい場合（現在値から見て概ね50%超）、段階適用の初日ステップ自体が既存の±15%キャップに引っかかり据え置き（revert）される可能性がある。従来は`apply_daily_rollouts()`が`_apply_single_param_update()`の`reverted_fields`戻り値を破棄していたため、この場合でも`pending_rollouts`のメタデータ上は「適用済み」と記録され、`total_days`到達時には実際には一度も反映されていないにもかかわらず完了扱いで削除されてしまう不整合があった。あわせて`update_log.jsonl`にも、実際にconfigへ書き込まれた値ではなくrevert前の意図した値がそのまま記録されていた。

対応として、`reverted_fields`（`"<profile_name>.<param_key>"`形式、`pending_rollouts`のキー形式`"profiles.<profile_name>.<param_key>"`との接頭辞差を吸収して照合）を実際に使用するよう修正し、該当パラメータがrevertされた場合は`pending_rollouts`エントリの`status`を`"reverted"`にして`day_index`を進めず、削除もしない（人間が気づいて対応するまで残り続ける）ようにした。`update_log.jsonl`の記録値も、意図した適用値ではなく実際にconfigへ反映された値（revert時は変更前の値と一致）に修正した。`test_review_pipeline_rollout.py`にrevertケースのテストを追加し、20件全てパスすることを確認済み。

**テスト**: `test_review_pipeline_rollout.py`（17件）。`calc_rolling_stats`（空ログ・他パラメータの除外・14日超の除外・平均標準偏差・3日乖離ちょうど20%）、`judge_outlier`（Zスコアちょうど2.0・超過・乖離ちょうど20%・超過・データ件数4件対5件・逆方向）、`create_or_replace_pending_rollout`（最小ステップ未満/ちょうど/エントリ内容）、`apply_daily_rollouts`（pending空・day_index進行・最終日到達）の境界値を網羅。

### 5.4.3 バックテスト検証ゲート（`ai_review/backtest_verifier.py`、2026-07-17実装）

**目的**: `imbalance_entry_threshold`/`take_profit_pct`/`stop_loss_pct`のいずれかを変更する提案について、`log/market_snapshot_YYYY-MM-DD.csv`（60秒間隔の板情報スナップショット）を使い、変更前・変更後それぞれの値で過去データに対する簡易シミュレーションを行う。変更後の方が明確に悪化していれば、その提案は適用せず変更前の値に据え置く。全ての変更提案（毎晩）が対象。

**既知の限界（重要）**: `market_snapshot`は60秒間隔のため、本番の板更新頻度（1秒間に複数回）に比べて粒度が粗い。60秒の間に価格がタッチして反転するようなケースは再現できず、あくまで「簡易」シミュレーションである。またメイカー/テイカー手数料・リベートはpnl計算に含めていない（価格変動分のみ）。

**ロジックの複製について**: `strategy_logic.py`の`evaluate()`は本番（`virtual_trader.py`の`on_orderbook_update`）では新規エントリー判定にしか使われておらず、保有中ポジションの利確/損切り判定・指値未約定時の約定/キャンセル判定は`_check_pending_fill`/`_check_active_position`という別の独立したコードが担当している。バックテスト側は新規エントリー判定のみ`evaluate()`を再利用し、残り2つは同じ条件式を`backtest_verifier.py`内に複製している（`_simulate_pending_fill`/`_simulate_active_position`）。**`virtual_trader.py`側のこれらの判定式を変更した場合、バックテスト側は自動追従しないため、あわせて`backtest_verifier.py`の修正が必要になる点に注意**（ドリフトリスクとしてコード内にも明記済み）。

**シミュレーション対象データ**: 過去30日分（`stability_check`ウィンドウと同じ日数）の`market_snapshot`のうち、対象プロファイルの時間帯窓（`start_time`〜`end_time`）に該当する行のみを使用する。日をまたいでポジションを持ち越さず、各日の時間帯窓の先頭でポジション無し状態から開始する。

**判定基準**:
- 変更前・変更後どちらかの取引件数が5件未満の場合、データ不足として判定せず（ゲートしない）従来通り適用する
- 変更前の合計損益（%）が正の場合、変更後が変更前の90%未満に下がったらゲート（ブロック）
- 変更前の合計損益（%）が0以下の場合、変更後がさらに悪化したらゲート（符号反転による誤判定を避けるため、正の場合と別ルール）

**ゲート時の挙動**: 対象プロファイルの該当パラメータのみ変更前の値に据え置く。他のプロファイル・他の安全装置（外れ値検知・段階適用・±15%キャップ・絶対範囲チェック）は独立して従来通り動作する。判定は外れ値処理後の値（段階適用の初日分適用値を含む、今夜実際に適用される値）に対して行う。

**通知**: ゲートが発生した夜はTelegram通知内で`backtest_gated:`セクションとして対象プロファイル・変更前後の合計損益（%）・据え置いたパラメータを表示する。データ不足でゲートしなかったケースは通知せず、`log/ai_review_decision_YYYY-MM-DD.json`の`backtest_results`にのみ記録する。

**テスト**: `test_backtest_verifier.py`（12件）。時間帯窓フィルタの境界値（start含む・end除外）、`StrategyConfig`構築（プロファイル値の反映・overrides優先・未指定はデフォルト）、シミュレーション（エントリー発生・利確でのpnl_pct一致・損切りでの実価格ベースpnl・指値未約定キャンセルの非カウント・日またぎでのポジションリセット）、ゲート判定（変更キー無し・データ不足・90%未満悪化・90%維持・非正値からの悪化・一部日欠損時の継続動作）を網羅。既存の`test_review_pipeline_rollout.py`（17件）とあわせて計32件パス確認済み。

### 5.5 ログ出力

| ファイル | 内容 |
|---|---|
| `log/ai_review_run_YYYY-MM-DD.log` | 夜間実行の全体ログ |
| `log/ai_review_summary_YYYY-MM-DD.json` | 集計サマリー |
| `log/ai_review_decision_YYYY-MM-DD.json` | Proposer/Skeptic/Moderatorの発言全文・最終判断 |
| `log/ai_validation_failures.jsonl` | バリデーション失敗の履歴 |
| `log/config_history.jsonl` | 設定変更履歴（差分・理由） |

### 5.6 APIキー管理
`ai_review/.env`（`.gitignore`対象）に以下を設定する。

```
GROQ_API_KEY=...
GEMINI_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

無料枠の目安: Groq（gpt-oss-120b）30 RPM/1,000 RPD/8,000 TPM、Gemini（Flash系）15 RPM/1,500 RPD/100万TPM。1晩あたりの呼び出しは数回程度のため、通常は無料枠内で収まる想定。

**Telegram設定の一本化（2026-07-15）**: 従来`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`は`ai_review/.env`と`scripts/.env`の両方に別々の値として保持されていたが（`scripts/telegram_notifier.py`が自分のディレクトリの`.env`を読む設計だったため）、片方だけ更新すると通知が届かなくなるリスクがあった。`scripts/telegram_notifier.py`の`ENV_PATH`を`ai_review/.env`を指すように変更し、`scripts/.env`は削除した。コード自体（`ai_review/telegram_notifier.py`と`scripts/telegram_notifier.py`）は重複したままだが、変更頻度が低い枯れたコードのため実害は小さいと判断し、設定値の一本化のみ対応した。

---

### 5.7 Telegram通知（失敗時のみ、実装済み）
`ai_review/telegram_notifier.py`（`send_telegram_message()`）により、AI議論パイプラインが最後まで正常に完了しなかった場合に通知される。

| 失敗パターン | 通知内容 |
|---|---|
| 集計サマリー未検出 | エラー通知 |
| Moderator呼び出し失敗（リトライ上限到達） | エラー通知 |
| Moderatorバリデーション3回失敗 | 見送り通知（安全に動作した結果であることが分かる文言） |
| `config.json`書き込み失敗 | エラー通知（重要） |

**（2026-07-25変更）Proposer/Skeptic呼び出し失敗の深夜リアルタイム通知は廃止した**。深夜に通知が来ても手動で対応する運用ではなく、再試行（5.4.1）で吸収できなかった場合は翌日以降の自動実行を待つ運用のため。失敗時は`error_kind`（タイムアウト／混雑／クォータ超過／その他）を`log/ai_review_decision_YYYY-MM-DD.json`に記録し、設定変更は行わない（従来どおり）。代わりに、前夜のAI議論の成否・失敗種別・設定変更概要を内部日次レポート（`build_daily_report.py`、4.7.3参照）でまとめて通知する。なお、この変更はAI議論のLLM呼び出し失敗の通知のみが対象で、異常検知アラート・サーキットブレーカー発動通知・エンジン系アラート等、他の既存通知は変更していない。

正常に適用できた場合は通知しない（日次レポート機能でまとめて知らせる、4.1・4.3・9章参照）。**通知文言・ログ全般で絵文字は使用しない**（10.3のインシデントを踏まえた運用ルール）。

異常検知アラート（取引頻度・損益急変の監視、7.8参照）・サーキットブレーカー発動通知（3.4.3参照）・日次レポート（9章参照）・バックテスト検証ゲートのブロック通知（5.4.3参照）はいずれも実装済み。

失敗通知を受け取った際の再実行手順は6.4「ダッシュボードからAI議論を手動リトライしたい場合」を参照。

---

## 6. 日常運用手順

### 6.1 ダッシュボードを開く
`run_dashboard.bat`をダブルクリックする。内部で以下を自動実行する。
1. `ensure_engine_running.ps1`でエンジンの起動確認（未起動なら起動）
2. `streamlit run dashboard.py`

### 6.2 手動でエンジンを再起動したい場合
```powershell
schtasks /Run /TN "BTC_Engine_Restart"
```
GMOのメンテナンス時間帯（05:55-06:30）以外に実行する場合、保有中ポジションは強制決済されずそのまま消える点に注意（プロセスが再起動されるため）。

### 6.3 AI議論パイプラインを手動実行したい場合
```powershell
schtasks /Run /TN "BTC_AI_Nightly_Review"
```
読み取り専用の集計＋API呼び出しのみで、エンジンには一切影響しない。過去日を指定して再実行することも可能。
```powershell
python ai_review/review_pipeline.py --target-date 2026-07-10
```

### 6.4 ダッシュボードからAI議論を手動リトライしたい場合（実装済み）
通信エラー・ファイルI/Oエラー等でAI議論パイプラインが正常に完了しなかった場合、PowerShellを手動で開かなくても、ダッシュボード（`dashboard.py`）上のボタンから再実行できる。

**操作**: ダッシュボードの「🔄 AI議論を再実行」ボタンをクリックする。

**内部動作**:
1. `subprocess.Popen` + `CREATE_NO_WINDOW`で`ai_review/run_nightly_review.ps1`をバックグラウンド起動する（PowerShell/コンソール画面は表示されない）
2. `run_nightly_review.ps1`側の既存のロック機構（`run_nightly_review.lock`、`LastWriteTime`が2時間以内なら実行中とみなしスキップ）がそのまま働くため、定時実行（03:30）と手動リトライが重なっても二重実行にはならない
3. ダッシュボードは同じロックファイルの`LastWriteTime`（ファイル更新日時）を参照して「実行中」/「実行可能」を判定し、実行中はボタンをdisabledにする
4. リトライを実行した日時をダッシュボード上に一時表示する

**設計上の注意（実装時に判明した問題）**: 当初、ダッシュボード側はロックファイルの中身（ISO8601タイムスタンプ）をパースして状態判定する実装だったが、`run_nightly_review.ps1`が`Set-Content -Encoding UTF8`でファイルを書き込むためUTF-8 BOM（`\ufeff`）が先頭に付き、パースに失敗して常に「実行可能」と誤表示される不具合があった。なお、この間もスクリプト側の多重起動防止（`LastWriteTime`ベース）自体は正常に機能しており、実際に二重実行が発生したことはない。対策として、ダッシュボード側の判定方法もスクリプト側と同じ`LastWriteTime`ベースに統一し、中身のパース処理自体を廃止した。これによりBOMの影響を受けなくなり、かつスクリプト側とUI側の判定基準が構造的に一致するようになった。

### 6.5 タスクの登録状況を確認する
```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like "BTC_*" }
schtasks /Query /TN "BTC_Engine_Restart" /V /FO LIST
```

### 6.6 現在の状態を確認する
```powershell
python -c "import sqlite3; conn=sqlite3.connect('live_state.db'); conn.row_factory=sqlite3.Row; print(dict(conn.execute('SELECT * FROM live_state WHERE id=1').fetchone()))"
```

### 6.7 異常な取引を検知した際に手動でシステムを緊急停止・再開する（実装済み）
ダッシュボード上から、保有ポジションを安全に決済したうえでエンジンを停止し、任意のタイミングで再開できる。

**操作**:
- 停止: ダッシュボードの「⏸ 緊急停止（安全決済）」ボタンをクリックする
- 再開: ダッシュボードの「▶ 再開」ボタンをクリックする（停止中のみ活性化）

**内部動作（停止）**:
1. ボタン押下でプロジェクトルート直下に`manual_stop.flag`が作成される
2. `virtual_trader.py`の`_update_maintenance_state()`（既存のメンテナンス判定と同じ箇所）がフラグを検知し、ポジション保有中であれば既存の`_force_close_maintenance()`（メンテナンス強制決済と同一ロジック）で決済する。この間、`_is_entry_blocked()`（既存のメンテナンス/セーフモード判定と同じ関数）にフラグ判定が追加されているため、新規エントリーも同時にブロックされる
3. `live_state.db`の`engine_status`カラム（`RUNNING`/`STOPPING`/`STOPPED`）が`STOPPING`に更新される
4. `trading_engine.py`のメインループ（1秒周期）がポジション解消を確認したら`engine_status`を`STOPPED`に保存し、既存のSIGINT/SIGTERM時と同じ安全停止経路（`shutdown_event`）でプロセスを終了する
5. `manual_stop.flag`が存在する間、`ensure_engine_running.ps1`（ダッシュボード起動時の自動起動チェック）は起動をスキップする。`restart_engine.ps1`（06:00定期再起動）は、残存プロセスがあれば停止のみ行い、新規起動はスキップする

**内部動作（再開）**:
1. 「▶ 再開」ボタン押下で`manual_stop.flag`を削除する
2. `ensure_engine_running.ps1`を`CREATE_NO_WINDOW`でバックグラウンド起動する
3. エンジン起動時に`engine_status`が`RUNNING`にリセットされ、`updated_at`の更新が再開されたことでダッシュボードが「稼働中」表示に戻る

**設計方針**: 決済ロジック・プロセス起動停止ロジックともに新規実装はせず、既存の仕組み（メンテナンス強制決済、`Find-AllEngineProcesses`/`Stop-AllEngineProcesses`、SIGINT/SIGTERM安全停止）をフラグ判定で分岐させる形で流用している。10.3の重複タスクインシデントを踏まえ、停止中は`ensure_engine_running.ps1`・`restart_engine.ps1`の両方が起動処理をスキップするようにしており、手動停止中に意図せず再起動されることはない。

---

## 7. トラブルシューティング / インシデント履歴

### 7.1 2026-07-03〜07-05: 二重起動によるデータ汚染事故

**症状**: 取引回数が数千件に達し、累積損益が-66,114円まで悪化（`win_count=3152, loss_count=3669`）。

**根本原因（2つ）**:
1. `restart_engine.ps1`のコメント内にあった登録例（`GMOTradingEngineRestart`）が、正式な`BTC_Engine_Restart`とは別に実際に登録されてしまい、**同じ内容のタスクが毎日06:00に2つ同時発火**していた
2. 旧バージョンの再起動スクリプトが「pidファイルに記録された1プロセスだけ」を停止対象にしており、過去の再起動処理の失敗で取り残されたプロセス（最古のものは7/3 13:24起動）を検知できなかった

**対応**:
- 全ての孤立プロセスを手動で特定・停止（`Get-CimInstance Win32_Process`でコマンドライン検索）
- `live_state.db`をバックアップの上リセット
- 汚染された07-03〜07-05の取引ログを`log/quarantined_20260703-05/`に隔離（AI議論の集計対象から自然に除外）
- 重複タスク`GMOTradingEngineRestart`を削除
- 恒久対策（下記7.2）を実装

**参考**: この期間の`realtime_trading_log_*.csv`は分析対象として使用しないこと。

### 7.2 恒久対策（実装済み・二重起動関連）
- `Find-AllEngineProcesses`/`Stop-AllEngineProcesses`: pidファイルに依存せず、コマンドラインで全`trading_engine.py`プロセスを検出・停止
- `restart_engine.ps1`自体の二重実行防止ロック（`restart_engine.lock`、5分しきい値）
- 全タスクスケジューラへの`MultipleInstances = IgnoreNew`設定
- `detect_orphan_engines.ps1`による1時間ごとの監視

### 7.3 2026-07-06: 動的サイジングの複利暴走＋絵文字printによるポジション未リセット事故

**症状**: 単一プロセスにも関わらず、朝08:20時点で決済回数15,080回、総資産152,543,932円という異常値。初期資金は5万円。

**根本原因（2つ、相互に影響し合った）**:
1. **動的ポジションサイジングバグ**: `virtual_trader.py`に「口座残高の20%を毎回の発注サイズにする」機能（`_calc_trade_size`, `POSITION_RATIO=0.20`）が、依頼していないにも関わらず追加されていた。Maker手数料0%・高い勝率という条件と組み合わさり、勝つたびに次の発注額が大きくなる複利ループが発生
2. **絵文字printによるポジション未リセットバグ（より深刻・真因）**: `_record_and_print`内の`print()`が絵文字（💰📉等）を含んでおり、ログファイルへの出力リダイレクト時にWindowsのcp932エンコーディングでエンコードできず`UnicodeEncodeError`が発生。この例外が`websocket_manager.py`のコールバック例外ハンドラで握りつぶされる際、`_exit_take_profit`等の中で例外発生箇所より後にある「ポジションをリセットする処理」が実行されないまま関数が中断していた。結果、決済済みのポジションが「保有中」のまま残り続け、次の板情報が届くたびに同じポジションが繰り返し「決済」判定され、残高・カウンターだけが実際のWebSocket更新頻度（1秒間に複数回）で無限に加算され続けた

診断の決め手は、`engine_2026-07-06.log`内の絵文字関連例外の出現回数（💰: 15,509回、📉: 554回）が、`win_count`/`loss_count`とほぼ一致したこと。

**対応**:
- プロセス停止、`live_state.db`のリセット、当日ログの隔離
- `_calc_trade_size`（動的サイジング）を削除し固定0.001 BTCに復帰
- 標準出力/標準エラーをUTF-8に固定（`sys.stdout.reconfigure`等）
- 状態変更処理（ポジションリセット）を、コンソールprintより必ず先に実行する順序に修正
- ログ・コンソール出力から絵文字を全面的に削除

### 7.4 恒久対策（実装済み・7.3関連）
- ログ・コンソール出力での絵文字使用を禁止（運用ルール化）
- 状態確定処理を表示処理より先に実行する設計原則の徹底
- `max_order_size_btc`を実効的な上限として`virtual_trader.py`側でクランプ、`PARAMETER_ABSOLUTE_BOUNDS`（0.01〜0.20）にも追加（詳細は5.4）
- 動的サイジングの再導入は保留とし、再導入する場合の要件を9章のTODOに明記

### 7.5 2026-07-07/08: 日次メンテナンス時間帯の新規エントリーブロック未実装

**症状**: 毎日05:55〜06:30のGMOコインメンテナンス時間帯中に、複数回ENTRY
（新規建玉）が記録され続けていた（例: 2026-07-08 06:00:13、2026-07-07
06:00:18/06:00:39/06:00:42）。ユーザーが実運用ログを目視確認して発覚。

**根本原因**: `virtual_trader.py`の`_is_entry_blocked()`には毎週土曜
09:00-11:00の週次メンテナンス判定（`_WEEKLY_MAINTENANCE_*`）のみが
実装されており、毎日05:55-06:30の日次メンテナンス判定が実装当初から
存在しなかった。仕様書上は「毎日05:55〜06:30」も記載されていたが、
実装がそれに追いついていなかった。

**発見の経緯**: ログ解析スクリプト（`verify_maintenance_entry_block.py`）
でメンテナンス時間帯内のENTRYを検出した後、`test_maintenance_entry_block.py`
でxfail(strict=True)を用いて「あるべき挙動」と「実際の挙動」の食い違いを
テストとして固定・可視化し、原因を「再起動直後の初期化順序の問題」ではなく
「日次判定そのものの未実装」であると切り分けた。

**対応**:
- `_DAILY_MAINTENANCE_START/END`（05:55 / 06:30）と`_is_daily_maintenance_window()`
  を、既存の週次判定と同じパターンで追加
- `_is_entry_blocked()` / `_update_maintenance_state()`に組み込み
- `test_maintenance_entry_block.py`のxfailマーカーを解除し、通常のリグレッション
  テストに昇格（11 passed）
- `verify_maintenance_entry_block.py`によるログベースの検証は変更なし

**教訓**: 仕様書に明記されている挙動でも、実装が追いついているとは限らない。
既存の類似ロジック（今回は週次メンテ判定）がある場合、それと突き合わせる
形でログを機械的に検証すると、こうした「片方だけ実装されている」漏れを
発見しやすい。

### 7.6 2026-07-10: restart_engine.ps1のプロセス起動失敗（Start-Process -ArgumentList にnull）

**症状**: `BTC_Engine_Restart`（06:00）実行時に`restart_engine.ps1`が例外で失敗し、
エンジンが停止したまま起動できなくなった。エラー: `パラメーター 'ArgumentList'
の引数を確認できません。引数が null または空であるか...`。この日06:00から
手動復旧まで約2.5時間、エンジンが完全に停止していた。

**根本原因**: `engine_process_utils.ps1`の`Start-EngineProcess`内で、
`cmd.exe /c`向けにリダイレクト付きコマンド文字列を組み立てる
`$redirectCommand = ...`という代入行が、直前の日本語コメント行と合わせて
PowerShell 5.1に誤解釈され（詳細機構は未確定。LF改行との組み合わせ、または
コメント行末の不可視文字の混入が疑われる）、実行されないまま
`Start-Process -ArgumentList "/c", $redirectCommand`が呼ばれ、
`$redirectCommand`が`$null`のままだったため。

**対応**:
- プロセス起動・ログの追記リダイレクト処理を、`cmd.exe`向け文字列組み立てを
  行うPowerShellロジックから、Python製の`process_launcher.py`
  （`subprocess.Popen`使用、Windows/Linux共通）に置き換えた
- `.ps1`ファイル群（`restart_engine.ps1`・`engine_process_utils.ps1`）の
  改行コードをCRLFに統一し、コメントは英語表記に変更（日本語コメント直後に
  重要な代入があるという壊れやすい構造を解消）
- 動作確認: エンジンプロセスが正常に起動し、ログが追記（上書きでなく）される
  ことを確認済み

**教訓**: OS固有のシェル・スクリプト構文へ依存するロジックは、たとえ動作実績
があっても、コメントの改行コードやエンコーディングといった目に見えにくい
要因で突然壊れうる。プロセス起動やファイルI/Oなど、言語標準ライブラリで
完結できる処理は、シェルスクリプトではなくPython側に寄せておくと、この種の
事故を構造的に減らせる。将来のLinux移行時にもそのまま流用できる副次効果もある。

### 7.7 2026-07-10: Proposer（Groq）のTPM超過による413エラー

**症状**: 03:30・08:33・09:00の3回、`review_pipeline.py`が
`Error code: 413 - Request too large for model openai/gpt-oss-120b ...
tokens per minute (TPM): Limit 8000, Requested 8453`で失敗し、AI議論が
3回連続で完了しなかった。

**根本原因**: Proposer（Groq）向けに渡している`reduced_summary`
（`build_proposer_prompt()`）が、バリデーション失敗時にAI議論パイプラインが
記録する`past_validation_failures`の`last_output`（Moderatorが出力した
生JSON全文）をそのまま含んでいたため、直近5件分の蓄積でプロンプトが肥大化し、
GroqのTPM上限（8000）を超えた。**Geminiのファイルサイズ制限とは無関係の、
GroqのTPM（レート制限の一種）超過であり、当初疑われたGemini File
API/Media APIへの切り替えは的外れな対応だった**（このエラーはGroq固有の
モデル名`openai/gpt-oss-120b`を含んでおり、Gemini呼び出しではない）。

**対応**:
- Proposer向け`reduced_summary`構築時、`past_validation_failures`の
  `last_output`を約300文字に切り詰め
- `call_groq()`の`max_tokens`を3000から2000に変更
- Skeptic/Moderator（Gemini）向けプロンプトは変更なし

**教訓**: LLM APIのエラーメッセージに含まれるモデル名・プロバイダ名を必ず
確認してから対応方針を決めること。「ファイルサイズ」という表現に引きずられて
Gemini側の対応策を検討しかけたが、実際にはGroq側のトークン数（テキスト量）の
問題だった。エラーメッセージの当該箇所を正確に読み取ることが遠回りを防ぐ。

**再発と追加対応（2026-07-15）**: 05:10、同種の413エラーが再発した（`Requested 8567`、初回の8453よりさらに増加）。調査の結果、`past_validation_failures`の件数上限（`_past_validation_failures_for_proposer`、Proposer向けは通常`limit=5`、プロンプトが12000文字超なら`limit=2`に自動縮小する既存の安全弁）は既に機能しており、今回の再発の主因ではなかった。実測（`ai_review_summary_2026-07-14.json`時点）で、Proposer向けプロンプト全体（約20,844文字）のうち、`recent_config_changes`（直近5件、約5,500文字）と`windows.stability_check.weekly_breakdown`（約4,000文字）が肥大化の主因と判明した。7/3頃（約1.3万文字）から7/14（約2.0万文字）にかけて継続的に増加するトレンドも確認されており、日々の運用データ蓄積に伴って構造的にプロンプトが肥大化する問題だった。

対応として`ai_review/prompts.py`のProposer向け構築のみを以下の通り修正した（Skeptic/Moderator向け・`past_validation_failures`の件数上限ロジック・`call_groq()`の`max_tokens`はいずれも変更なし）。

- `stability_check.weekly_breakdown`をProposer向けから完全に除外し、集計値のみを渡すようにした。既存の`regime_reference`（Proposerには集計値のみ、Skeptic/Moderatorには日次生データも、4.2節）と同じ方針
- `recent_config_changes`をProposer向けには直近5件→3件に削減し、各エントリの`reason`を約300文字に切り詰め（`past_validation_failures`の`last_output`切り詰めと同じ考え方）
- Proposer向け`reduced_summary`の構築完了時、文字数をログ出力するようにした（`[INFO] proposer reduced_summary chars=...`）。再発時に推測でなくログから直接原因を確認できるようにするため

修正後の実測（同じく`ai_review_summary_2026-07-14.json`基準）は、prompt全体で約15,652文字（削減前比約5,200文字減）となり、目安としていた15,000〜16,000文字の範囲に収まることを確認した。テストは`test_review_pipeline_rollout.py`に追加し19件パス。

**教訓（追加）**: 1件あたりのサイズを切り詰める対症療法（初回対応）だけでは、含める件数や参照範囲が運用データの蓄積とともに緩やかに増加し続ける構造的な問題は解決しない。今回のようにプロンプトサイズの増加トレンドは、事後に文字数ログを見ないと気づけないため、再発防止のログ出力を合わせて実装した。

**再発と根本対応（2026-07-17）**: 03:30、3度目の413エラーが再発した（`Requested 10361`、2回目の8567からさらに増加）。実測の結果、7/15対策済みの箇所（`weekly_breakdown`除外・`recent_config_changes`3件化）に適用漏れは無く、真因は別にあった。同日中に追加された`market_trades`/`market_depth`/`market_volatility`（市況集計、5.2.1〜5.2.3参照）が、`rule_review`（14日窓）・`stability_check`（30日窓）・`regime_reference`（90日窓）の3つのウィンドウすべてに、実質同一内容のまま三重に含まれていたことが主因（合計約1.2万文字）。データ蓄積が浅い（該当データが1日分しかない）ため3窓の計算結果が偶然完全一致していたが、設計上は日数が異なれば分岐しうる値であることを確認した上で、以下の対応を行った。

- `market_trades`/`market_depth`/`market_volatility`ブロックは、Proposer向けには`rule_review`（14日窓）のみに残し、`stability_check`・`regime_reference`からは除外
- `current_config.pending_rollouts`をProposer向けには詳細JSONから要約（`{count, items:[{path, status, day_index, total_days}]}`）に圧縮（常時適用）
- 上記2点を適用してもなお安全ライン（`PROPOSER_COMBINED_SAFETY_CHARS=18000`文字、system+prompt結合後）を超える場合に発動する**段階的な安全弁**を追加:
  - step_a: `recent_config_changes`を3件→2件、`reason`を300文字→200文字にさらに削減、`[WARN]`ログ
  - step_b: `rule_review`（14日窓）の`market_*`ブロックも除外、`[WARN]`ログ
- 実測（`ai_review_summary_2026-07-17.json`）では、`pending_rollouts`圧縮のみでは20,078文字、+step_aで19,167文字、+step_bで15,264文字となり、**step_a・step_b両方の発動でようやく安全ライン内**に収まった。TPM概算で入力約4,400＋`max_tokens=2000`で計約6,400（Limit 8000に対して余裕あり）
- Skeptic/Moderator（Gemini）向けプロンプト・`max_tokens`（2000）は変更していない

**検討し、見送った代替案**: Proposerも Groq から Gemini に切り替える案を検討したが、見送った。5.4節の「異なるモデル系統（Groq/Gemini）を使うことで、片方の推論の癖・過学習をもう片方がチェックできる」というクロスチェック設計そのものが崩れるため（提案側とチェック側が同一モデル系統になる）。トークン制約から解放される利点はあるが、安全設計上のトレードオフが大きいと判断し、今回は情報圧縮側で対応した。

**残された懸念**: 当初は温存する方針だった`rule_review`（14日窓）の`market_*`まで、安全弁のstep_bとして削られる設計になった。今回の実測ではstep_a・step_bの両方が発動して初めて安全ラインに収まっており、これが常態化する（毎回step_bまで発動する）ようであれば、それは「安全弁」ではなく「通常経路」であり、Proposerは常に市況データ抜きで判断していることになる。今後の運用ログ（`[WARN]`の発生頻度）を見て、頻発するようなら別途の根本対応（3窓構造そのものの見直し等）が必要（9章に記録）。

**教訓（再追加）**: 3回の再発を経て、対症療法（1件あたりの切り詰め）→構造要因の特定（3窓への重複投入）→動的な安全弁（段階的自動削減）という順に対応が進化した。運用データが蓄積し続ける限りプロンプトサイズは今後も増加傾向にあるため、固定的な切り詰めルールをその都度手動で調整するより、送信直前に実測して自動で収める安全弁を持たせておく方が、同種の再発に対して構造的に強い。

**テスト**: `test_review_pipeline_rollout.py`ほかに追加、38件パス（`pending_rollouts`圧縮、安全ライン以下での非発動、安全ライン超過時のstep_a/step_b発動、`[WARN]`ログ出力を含む）。

### 7.8 2026-07-12: 仕様書の「未実装」記載と実装の乖離（check_trading_anomaly.py）

**症状**: 案B実装のための参照箇所洗い出し中、`scripts/check_trading_anomaly.py`が`live_state.db`を直接参照する実装済みコードとしてヒットした。しかし9章には「異常検知アラート（`scripts/check_trading_anomaly.py`として設計済み・未実装）」と記載されており、仕様書とコードの実態が食い違っていた。中身を確認したところ、閾値判定・アラートのクールダウン（2時間）・状態永続化（`anomaly_state.json`）まで含む、稼働可能な完成度のコードだった。

**原因**: 実装が完了した後、9章の該当記述を更新し忘れていたと推測される（いつ実装されたか、タスクスケジューラに登録済みかどうかは今回のセッションでは確認していない）。

**対応**: 9章の記載を「実装済み、ただしタスクスケジューラへの登録状況は未確認」に修正した（9章参照）。案Bの対象ファイル一覧にも追加し、`live_state.db`・`anomaly_state.json`両方のパス定数を`runtime/`配下に修正した。

**教訓**: 8章の「仕様書の記述と実装の突き合わせ」がまさに当てはまる事例。今回は案Bの参照箇所洗い出しという別の作業の副産物として偶然発見できたが、機械的な洗い出し作業のたびに「ついでに仕様書との整合性も確認する」姿勢が有効であることを示している。

### 7.9 2026-07-12: docker-compose.test.ymlでのmanual_stop.flag未共有（実害発生前に発見）

**症状**: `runtime/`共有方式への移行作業中、修正前の`docker-compose.test.yml`が`live_state.db`のみを単一ファイルとしてbind mountしており、`manual_stop.flag`・`trading_engine.pid`はどちらのコンテナにもマウントされていなかったことに気づいた。`engine`コンテナと`dashboard`コンテナは別々のファイルシステムを持つため、この状態では**ダッシュボードの緊急停止ボタンを押してもengineコンテナ内のプロセスには一切伝わらなかった可能性がある**。

**原因**: 2026-07-11のDocker検証時点で、Windows特有の落とし穴（存在しないファイルをbind mountすると空ディレクトリが作られ`manual_stop.flag`の存在判定が誤ってTrueになる、4.5節参照）を避けるため、`manual_stop.flag`のマウント自体を意図的に見送っていた。この暫定対応がテスト環境に残ったままだった。

**対応**: `runtime/`フォルダごと共有する方式に変更したことで、`manual_stop.flag`・`trading_engine.pid`も自動的に共有対象になり解消された。

**教訓**: テスト/仮想モードで実害は出ていなかったが、本番同様の構成で緊急停止の実地テストをしていなければ気づかないまま本番投入されていた可能性がある。安全装置に関わる設定変更（今回で言えばbind mount対象の見送り）は、その場しのぎの対応で終わらせず、後続タスク（案B）の中で必ず解消したかを確認する運用が重要。

### 7.10 2026-07-12: `runtime/`統合時の`.gitignore`パターン消失、および`run_nightly_review.sh`のCRLF混入

**症状1（.gitignore）**: `git add --renormalize .`実行後、`live_state.db.bak_20260705`が`.gitignore`で無視されずuntrackedとして検出された。原因は、`runtime/`統合時に旧来個別列挙していた`live_state.db.bak*`パターンが`runtime/*`への一本化の過程で消えていたこと（バックアップファイルはプロジェクトルート直下に生成される運用のため、`runtime/*`ではカバーできない）。`live_state.db.bak*`パターンを復活させて解消。あわせて`.gitignore`の追記時に末尾改行がなく既存最終行と結合してしまう事象も発生し、ファイル全体をBOMなしUTF-8で書き直して解消した。

**症状2（CRLF）**: `ai_review/run_nightly_review.sh`を`bash -n`で構文チェックしたところ、15行目`log() {`直後に`\r`が混入しておりsyntax errorとなった。Windows側エディタ（Cursor）での保存時にCRLFへ変換された可能性が高い。`tr -d '\r'`で除去して解消し、再発防止として`.gitattributes`に`*.sh text eol=lf`を追加した。

**教訓**: `runtime/`統合のような`.gitignore`の構造変更（個別列挙→ワイルドカード一本化）は、既存パターンの意図しない消失を伴いやすい。変更後は`git check-ignore -v`で主要な無視対象ファイルを個別に再確認する運用が有効。また、Windows環境でシェルスクリプトを編集する場合は、エディタの改行コード設定または`.gitattributes`による強制LF化を初期段階で入れておくことが望ましい。

### 7.11 2026-07-12: サーキットブレーカー発動→再開後、`check_trading_anomaly.py`が誤検知しうる問題

**症状**: 16:28、1分間に5回以上の発注で3.4.1のサーキットブレーカーが発動し`manual_stop.flag`によりエンジンが緊急停止。その後手動で再開したところ、17:25の`BTC_Anomaly_Check`（定期実行）が`delta_trades=-20`という負の値を記録した（`runtime/anomaly_state.json`より、リセット前は取引21件・累積損益+55.349円だったのに対し、リセット後は1件・-23.601円まで下がっていたことが判明）。今回はたまたま閾値（`trades_per_hour > 60`または`pnl_per_hour < -3000`）に届かず実害はなかったが、リセット直前の含み益・取引件数次第では「損失急拡大」の誤アラートが送信されていた可能性がある。

**原因**: `check_trading_anomaly.py`は`live_state.db`の`win_count`・`loss_count`・`cumulative_pnl`を単純に前回チェック時点の値と差分計算する設計だった。これらの値がエンジン再起動（サーキットブレーカー起因・`BTC_Engine_Restart`の定例実行いずれも含む）でリセットされる場合、差分が実態と無関係な大きな負の値になり、誤って「取引急減・損失急拡大」と判定されうる構造的な弱点があった。

**対応**: `current.total_trades < prev_total_trades`（＝カウンターが減少している＝リセットが起きたと推定できる状態）を検知した場合、閾値判定・アラート送信をスキップし、現在値をそのまま新しいベースラインとして保存する分岐を追加した（`main()`内、初回実行時と同様の扱い）。Cursorによる修正後、通常パス・リセット検知パスの両方、および既存55件のユニットテストで動作確認済み。

**教訓**: 「前回値との単純な差分」で異常検知するロジックは、監視対象の値が正当な理由でリセットされうる場合（今回のエンジン再起動のように）、リセットそのものを異常と誤解釈するリスクを常に持つ。サーキットブレーカーや定例再起動など、監視対象システムが意図的に状態をリセットする箇所を洗い出し、監視ロジック側にリセット検知の分岐が必要かを確認する視点が今後も有効。

### 7.12 2026-07-14: docker-compose.test.ymlでのon-failure無制限リトライ発見（実害発生前に発見）

**症状**: 4.5.5③の統合テストシナリオ5（異常終了時の`on-failure`再起動確認）で、`docker_test/config/config.json`を意図的に壊し`docker compose restart engine`したところ、`RestartCount`が上限なく増加し続けることを確認した。テストでは5回程度で手動確認・復元したが、`restart: on-failure`に最大回数を指定していなかったため、理論上は障害が続く限り無制限に再起動を繰り返す設定になっていた。

**原因**: 4.5.3で本番用`docker-compose.yml`を新規作成した際、「終了コード0（緊急停止）では再起動しない／終了コード1以上（クラッシュ）では再起動する」という設計の正しさ（on-failureの選定根拠）は確認していたが、**再起動できる場合の上限回数**は検討していなかった。

**対応**: `engine`・`dashboard`両サービスの`restart`を`on-failure:5`に変更（4.5.3参照）。あわせて、上限到達＝人間の介入が必要な状態を検知しTelegram通知する`scripts/check_engine_crash_loop.py`を新規実装した（詳細は4.5.6）。

**教訓**: 7.9（`manual_stop.flag`未共有）と同種の「実害の出ていない設計上の穴を、本番投入前の統合テストで発見できた」事例。`restart`ポリシーのような「一見動いているように見える」設定は、正常系だけでなく異常系を意図的に発生させて確認しないと、上限の有無のような落とし穴に気づきにくい。

### 7.13 2026-07-13〜14: `imbalance`ノイズによるサーキットブレーカー頻発

**症状**: 7/13の9時〜12時頃に3回、7/14の9時頃に2回、3.4.1のサーキットブレーカー（60秒あたり発注5回超）が発動し緊急停止した。通知にすぐ気づけない場合は再開が遅れ、頻発によりダッシュボードの確認・手動再開という運用負荷も増していた。

**原因**: `realtime_trading_log`を確認したところ、いずれのケースも価格自体は一切動いておらず、板の`imbalance`（買い圧力の指標）だけが`daytime`プロファイルのエントリー閾値（0.55）付近を1秒未満の間隔で往復し、「エントリー→（閾値割れで）キャンセル→（閾値回復で）再エントリー」を繰り返していた。想定外の暴走・バグではなく、**imbalanceという1ティックごとに再計算される指標を、ヒステリシスなしでそのままエントリー/キャンセル判定に使っていたことによる正常な反応**だった。

**対応**: `virtual_trader.py`に同一価格キャンセル後の再エントリークールダウン（2秒、固定値）を追加した（詳細は3.4.7）。サーキットブレーカー本体（`check_order_rate_limit()`）や`config.json`のパラメータは変更していない。「通知にすぐ気づけない場合は再開が遅れる」という運用面の課題自体は、2026-07-14にサーキットブレーカー自動発動時のTelegram通知（3.4.3参照）を追加して解消した。

**検討したが採用しなかった案**: ①`imbalance_entry_threshold`とは別に`imbalance_cancel_threshold`を設けるヒステリシス方式も検討したが、5.4.2のAI夜間レビューがエントリー閾値のみを調整対象にしているため、2つの閾値の関係が崩れるリスクがあり見送った。②サーキットブレーカー起因の停止だけを区別して自動再開する案も検討したが、3.4.3の「新たな停止経路は作らない」という既存方針からの転換になるため、まずは軸1（発注頻度そのものを減らす）の効果を見てから判断することにした。

**教訓**: 板情報から算出される指標（imbalance等）は、閾値付近では正当な理由なく高頻度に反転しうる。エントリー/キャンセルの判定に使う場合、閾値をまたいだ瞬間に即座に反応する設計は、意図しない発注頻度の増加とサーキットブレーカーの誤発動（正確には「誤」ではなく「安全装置としては正しい反応だが運用上望ましくない頻度」）を招きやすい。同種の指標を新たに判定に使う際は、最初からクールダウンやヒステリシスの要否を検討する視点が有効。

### 7.15 2026-07-15: `reset_trading_state.py`のスキーマが`trading_engine.py`と乖離

**症状**: VPS契約待ちの間に進めていた技術的負債整理の一環で`scripts/reset_trading_state.py`を確認したところ、3.4.8で`live_state.db`に追加した`trading_day_date`/`daily_start_balance`/`daily_realized_pnl`の3カラムが、同スクリプトの`CREATE TABLE`文・`INSERT`文双方に含まれていないことが判明した。修正中にさらに、これら3カラム追加以前から存在していた`engine_status`カラムも同様に漏れていることが分かった。

**原因**: `live_state`テーブルのスキーマ定義が`trading_engine.py`（`_CREATE_TABLE_SQL`・`_ensure_live_state_schema()`によるマイグレーション）と`reset_trading_state.py`の2箇所に分散しており、片方にカラムを追加した際にもう片方への反映が漏れる構造だった。ただし`trading_engine.py`側の`_ensure_live_state_schema()`が起動時・書き込み時に毎回`ALTER TABLE`で不足カラムを補い、`engine_status`・`daily_realized_pnl`はNULLの場合に自動補正するため、実害は「リセット直後からエンジン起動までの間に他プロセスがこれらの欠けたカラムを参照した場合」に限定されていた。

**対応**: `trading_engine.py`側の全カラムと突き合わせ、`reset_trading_state.py`の`CREATE TABLE`・`INSERT`文に不足していた4カラムを追加した。手動リセット実行後、`PRAGMA table_info`で全カラムの存在を確認済み。

**教訓**: 同じテーブルのスキーマ定義が複数ファイルに分散している場合、片方への変更時に他方への影響を機械的に確認する手順（例: 変更時のチェックリストに「他にこのテーブルを`CREATE TABLE`している箇所がないか」を含める）がないと、同種の抜けが繰り返される。今回は自己修復機構（`_ensure_live_state_schema()`）があったため実害を免れたが、次に同じテーブルへカラムを追加する際は両ファイルを同時に確認すること。

### 7.16 2026-07-15: `apply_daily_rollouts()`が`reverted_fields`を握りつぶしていた

**症状**: 5.4.2の段階適用（`pending_rollouts`）について、9章に記録されていた「revertされたパラメータが`pending_rollouts`側では適用済みと誤記録される」という既知の残課題を解消しようとしたところ、想定より根が深い実装上の問題が2点見つかった。

**原因**: `apply_daily_rollouts()`が`_apply_single_param_update()`の戻り値のうち`reverted_fields`を`_reverted`という名前で受け取りながら一切使用せず捨てていたため、±15%キャップでrevertされた変更でも`pending_rollouts`のメタデータ上は正常に段階適用が進んだものとして扱われていた。あわせて、`update_log.jsonl`への記録も、実際にconfigへ書き込まれた値ではなくrevert前の意図した適用値をそのまま記録しており、AI議論が参照する`calc_rolling_stats()`の統計データ自体が汚染されうる状態だった。

**対応**: `reverted_fields`を実際に使用し、該当パラメータがrevertされた場合は`pending_rollouts`エントリの`status`を`"reverted"`にして`day_index`を進めず削除もしない（人間が気づくまで残り続ける）よう修正した。`update_log.jsonl`の記録値も、実際にconfigへ反映された値を取得し直すよう修正した。`reverted_fields`のキー形式（`"<profile_name>.<param_key>"`）と`pending_rollouts`のキー形式（`"profiles.<profile_name>.<param_key>"`）の接頭辞差を吸収する照合処理も追加した。`test_review_pipeline_rollout.py`にrevertケースのテストを追加し、20件全てパスすることを確認済み。詳細は5.4.2参照。

**教訓**: 戻り値をアンダースコア接頭辞（`_reverted`）で受けて捨てる書き方は、「後で使うかもしれないが今は使わない」という意図を示す一方、今回のように「本来使うべきだったのに実装時に見落とした」ケースとの区別がコード上つかない。安全装置の判定結果（reverted等）を戻す関数を呼ぶ箇所では、戻り値を捨てる場合でも一度は用途を確認する習慣が有効。

### 7.17 2026-07-15: 監視系スクリプト自体の死活監視を追加

**背景**: 9章に低優先度課題として記録されていた「`check_trading_anomaly.py`・`check_engine_crash_loop.py`・`check_csv_db_consistency.py`自体が動かなくなった場合を検知する仕組みがない」問題に対応した。

**対応**: 2段階で実装した。①各`.py`が正常終了するたびに`runtime/monitor_heartbeats.json`へ自分の最終正常終了時刻を記録する。②新規`scripts/check_monitor_heartbeats.py`が許容間隔（クラッシュループ検知は20分、それ以外は2時間）を超えて更新が無いスクリプトを検知しTelegram通知する（`check_engine_crash_loop.py`の`crash_loop_notified_<service>.flag`と同じ重複防止フラグ方式を踏襲）。あわせて3つの`.ps1`ラッパーの`catch`ブロックにも即時Telegram通知を追加し、`BTC_Monitor_Heartbeat_Check`として15分毎にタスクスケジューラへ登録した（GUI登録、詳細は4.1・4.3）。

**判明した限界**: 実装過程で、3つの`.ps1`ラッパーはいずれもPython側の終了コード（`sys.exit(1)`等、例外を投げない正常終了パスとしての異常系）を`throw`し直していないため、`catch`ブロックが発火しないことが判明した。この場合もハートビートは更新されないため`check_monitor_heartbeats.py`による遅延検知では最終的に拾われるが、即時通知の対象からは漏れる（9章に低優先度課題として記録）。またこの死活監視の仕組み自体も、Windowsのタスクスケジューラのサービス自体が全滅するような障害には無力である点は設計上の限界として認識している。

**教訓**: 「異常を検知して通知する」仕組みを作る際、その仕組み自身が動いているかを別の経路で確認する視点が必要になる（5.7節のAI夜間レビューの失敗通知は元々この視点を含んでいたが、後発の監視スクリプト群には反映されていなかった）。新しい監視・通知の仕組みを追加するたびに「これ自体が沈黙した場合に気づけるか」を確認する習慣が有効。

### 7.18 よくあるエラーと対処

| 症状 | 原因 | 対処 |
|---|---|---|
| `live_state.db`が削除できない | Streamlitダッシュボードやその他プロセスがファイルを開いたまま | `Get-CimInstance Win32_Process`でstreamlit/dashboard.py関連プロセスを確認・停止 |
| `sqlite3.OperationalError: no such table` | プロジェクトルート以外のディレクトリで実行している | `cd`してから実行するか、フルパスを指定する |
| `taskkill`が失敗するのに次のプロセスが起動してしまう | 停止確認をせず無条件で新規起動していた（旧バージョンの不具合） | 現行版では解消済み。停止失敗時は起動しない設計 |

### 7.19 2026-07-15: `reset_trading_state.py`のCSV退避により当日の取引データがAI議論の集計から欠落

**症状**: 0時台に3.4.1のサーキットブレーカー（60秒5発注超）が2回連続で発動し、いずれも発動直後に手動でエンジンを再起動して運用を再開した。その後7.15のスキーマ不整合修正の動作確認のため05:46頃に`scripts/reset_trading_state.py`を実行したところ、`log/realtime_trading_log_2026-07-15.csv`の内容（00:29:05〜、114行）が`log/reset_backup_20260715-054617/`へ丸ごと移動され、`log/`直下には実行後の新規取引（05:56:13〜、64行）しか残らない状態になった。

**原因**: `reset_trading_state.py`は`live_state.db`のリセットとあわせて、当日の`realtime_trading_log_*.csv`を`shutil.move`でbackup_dirへ退避する実装になっていた（DB側は`shutil.copy2`によるコピー＋書き換えだったが、CSV側のみ移動）。この挙動はオプションで制御できず、常時発生する。今回のように「新しい取引日を開始するための日次リセット」ではなく「スキーマ確認のための動作確認目的」で実行した場合にも同じ副作用が及び、当日データがまるごと`log/`直下から失われた。

**影響範囲の確認**: `ai_review/build_ai_review_summary.py`の`load_trade_rows()`は`LOG_DIR / f"realtime_trading_log_{date}.csv"`という固定パスのみを参照し、`glob`/`rglob`は使用していないため、`log/reset_backup_*/`配下は一切参照されない（`log/quarantined_*/`と同様の扱い）。このため対応しないまま放置した場合、`anomaly_check`（前日1日）・`rule_review`（14日）・`stability_check`（30日）・`regime_reference`（90日）のいずれのウィンドウでも、2026-07-15を含む集計は「退避後の薄いCSV」のみを参照することになり、0時台の2回のサーキットブレーカー発動という重要なリスクシグナルがAI議論から欠落するところだった。

**対応**:
1. **緊急対応**: 03:30のAI議論より前に、`log/reset_backup_20260715-054617/realtime_trading_log_2026-07-15.csv`（114行）と退避後の`log/realtime_trading_log_2026-07-15.csv`（64行）をタイムスタンプ順にマージし、`log/realtime_trading_log_2026-07-15.csv`（ヘッダ含め179行、00:29:05〜11:23:53、`trade_id`重複なし）として復元した。`log/reset_backup_20260715-054617/`自体は控えとして削除せず残置している。
2. **恒久対応**: `reset_trading_state.py`のCSV退避処理を`shutil.move`から`shutil.copy2`に変更した。これにより当日CSVはbackup_dirへコピーされる一方、`log/`直下の元ファイルはそのまま残り、`build_ai_review_summary.py`が参照できなくなることはない。ログ出力の文言も`[reset] archived log: ...`から`[reset] copied log to backup: ...`へ変更した。`live_state.db`側の`copy2`（退避＋リセット）は変更していない。関連テストは無く、修正も不要だった。

**教訓**: 「アーカイブ」という言葉には「移動（元を消す）」と「コピー（元を残す）」の2通りの意味が混在しうる。`live_state.db`は「リセットする対象そのもの」なのでアーカイブ＝退避＋書き換えで正しいが、`realtime_trading_log`は「リセット対象ではなく、他プロセス（AI議論の集計）が継続参照する対象」であるため、同じ言葉・同じ実装パターンを流用すると意図しない欠落を招く。1つのスクリプトが複数の状態ファイルを扱う際は、ファイルごとに「リセットされるべき状態」なのか「リセットとは無関係に参照され続けるログ」なのかを区別し、退避方式（move/copy）を個別に検討する必要がある。また、スキーマ確認のような一時的な動作確認目的であっても、リセット系スクリプトの実行は本番の状態ファイルに実際の副作用を及ぼす点に注意が必要（可能であれば`docker_test/`等の隔離環境で確認する方が安全）。

### 7.20 2026-07-19: `jpy_balance`等がエンジン再起動のたびにリセットされる問題（実害発生前に発見）

AIコードレビューで発見。詳細・修正内容は3.4.13を参照。要点のみ記す。

**症状（想定）**: real mode運用中にエンジンが再起動すると、`VirtualTrader.jpy_balance`が実際の口座残高ではなく固定値5万円へリセットされ、3.4.2のreconciliationチェックが2回連続で不一致と判定し、`manual_stop.flag`（`reconciliation_mismatch`）を誤発動する。virtual mode運用中は実害なし（現在はvirtual modeのため、今回は実際には発生していない）。

**原因**: `VirtualTrader.__init__`が`jpy_balance`を常にコンストラクタ引数`initial_jpy`（固定値5万円）で初期化する実装になっており、`live_state.db`から復元する経路が存在しなかった。3.4.8で`daily_start_balance` / `daily_realized_pnl`の復元は実装済みだったが、`jpy_balance`本体は対象外のままだった。

**対応**: 3.4.13参照。`restore_persisted_account_state()`を新設し、`initialize_daily_loss_state()`より前に呼ぶよう`trading_engine.py`の`main()`を修正した。

**教訓**: 「日次サイクルの基準値（`daily_start_balance`）」と「口座残高そのもの（`jpy_balance`）」は別々の永続化対象であり、片方だけを復元する実装では見た目上動いているように見えても、reconciliationのような別の安全装置と組み合わさって初めて症状が顕在化する。real mode移行前のコードレビューで確認できたことは幸運であり、本番投入前には同様の観点（「再起動をまたいで本当に引き継がれるべき状態は何か」の棚卸し）を他の永続化項目についても確認する必要がある。

### 7.21 2026-07-19: Public WebSocket板情報メッセージの例外未保護（実害発生前に発見）

AIコードレビューで発見。

**症状（想定）**: `websocket_manager.py`の`WebSocketManager._on_message()`（Public、板情報）は、`json.loads(message)`および`bids[0]["price"]`等の辞書アクセスに例外保護がなかった。同ファイル内の`_on_trades_message()`（3.3.1）や`PrivateWebSocketManager._on_message()`（3.4.11）は同種の処理を`try/except`で保護しているのに対し、板情報側のみ無防備だった。GMOから想定外形式のメッセージ（ハートビート・購読確認・将来のAPI仕様変更等）を受信した場合、コールバックスレッド内で例外が発生し、板情報の更新が静かに止まるリスクがあった。

**対応**: `_on_message()`を2箇所で保護するよう修正した。
- `json.loads(message)`を`try/except json.JSONDecodeError`で囲み、失敗時は`[WebSocketManager] orderbook JSON parse error: {exc}`を出力してreturnする
- `OrderbookSnapshot`構築とdepth集計を`try/except (KeyError, IndexError, TypeError, ValueError)`で囲み、失敗時は`[WebSocketManager] orderbook parse error: {exc}`を出力してreturnする

いずれもクラッシュせず処理を継続する。エラーメッセージの書式は既存の`_on_trades_message()`の`trades parse error`に合わせた。

**教訓**: 同一ファイル内で類似の処理（Public/Private、trades/orderbook）を実装する際、片方にだけ例外保護が入り、もう片方が漏れるということが起こりうる。「隣にある似た処理と保護レベルが揃っているか」は、コードレビュー時に機械的にチェックできる観点として有効だった。

### 7.22 2026-07-19: `build_change_outcomes.py`の変更前(before)集計がconfig_versionで絞られていなかった

AIコードレビューで発見。

**症状**: `_select_rows_for_window()`は、変更後(after)の取引データを`config_version == after_version`で厳密に絞り込む一方、変更前(before)の取引データは`before_version`という引数を受け取っていながら使用せず、時間範囲とプロファイル名のみで絞り込んでいた。5.4.2のpending_rollout（3日間の段階適用）では1ステップごとに`config_version`が更新されるため、評価ウィンドウの「before」側に複数の異なる中間バージョンの取引が混在しうる状態だった。

**対応**: `before_rows`の絞り込みに`r["config_version"] == before_version`を追加し、`after_rows`と同じ基準に揃えた（`ai_review/build_change_outcomes.py`）。

**この修正による挙動変化（意図的なもの）**: 頻繁に変更されるパラメータ（特にpending_rollout中間ステップ）は、「before」側のサンプル数が減り、`classify_confidence()`が`insufficient`を返すケースが増える。これは「不正確な比較をするより不確実性を正直に表明する」という設計判断によるもので、想定通りの変化である。

**検討したが採用しなかった代替案**: 「rolloutが完了(`completed`)するまで評価対象から除外する」案も検討したが、`create_or_replace_pending_rollout()`は既存rolloutが完了する前に新しい外れ値提案が来た場合、現在地点から新しいtarget_valueへ向けてday_index=1からやり直す仕様のため、連日outlier判定が続くとそのパラメータが`completed`に到達する日が永遠に来ず、change_outcomesに一切記録されなくなるリスクがあることが判明し、不採用とした。既存の`_next_profile_change_ts()`による「次の変更が来たら評価を確定する」仕組みが、この問題を自然に回避していることを確認済み。

**テスト**: `test_build_change_outcomes.py`に`test_before_window_uses_config_version_match_like_after`を追加。既存の`test_first_profile_change_has_no_effective_start_limit`は、テストデータのconfig_versionを`previous_version`に合わせる形で新仕様に対応させた。5件すべてパス。

---

### 7.23 2026-07-22: pytestによる本番取引CSVの汚染（テスト分離バグ）

4.6の公開レポート機能テスト中に発見。

**症状**: 本番の`realtime_trading_log_YYYY-MM-DD.csv`に、価格990万/1000万といったきりの良いテスト値、`profile=default/full_day`、数量0.03 BTCといったテスト固有の行が混入していた（16:01:01、同一秒に複数行）。決済理由（`FORCE_CLOSE_MAINTENANCE`/`STOP_LOSS`）だけが異なりほぼ同一内容の行もあり、当初は決済ロジックの二重発火（実際のバグ）を疑ったが、Cursorの調査により、テスト起因のデータ混入と判明した。

**原因**: `virtual_trader._record_and_print()`は`LOG_DIR`（`Path(__file__).resolve().parent.parent / "log"`）経由で`realtime_trading_log_YYYY-MM-DD.csv`に追記する設計になっている。`test_virtual_trader.py`は`_enter_long()` / `_exit_stop_loss()` / `_force_close_maintenance()`等を呼び出す際、この`LOG_DIR`を差し替えていなかったため、テスト実行のたびに本番CSVへ直接書き込んでいた。

**対応**:
1. `test_virtual_trader.py`にautouseフィクスチャ`isolated_trade_csv_log_dir`を追加し、`monkeypatch.setattr(virtual_trader_module, "LOG_DIR", tmp_path / "log")`で一時ディレクトリへ誘導
2. ルート直下に`conftest.py`を新規追加し、pytestセッション終了時に本番取引CSVのsha256/mtimeが不変であることを検証する仕組みを常時有効化（`live_state.db`はエンジン稼働中に外部更新され誤検知するため、`PYTEST_GUARD_LIVE_STATE=1`かつエンジン停止時のみ厳密チェックする設計）
3. `tests/test_production_data_integrity.py`を新規追加し、上記ガード設定自体を検証
4. 汚染された本番CSVの該当行は手動で削除済み（テスト用パターンでの機械的な削除スクリプトは今回は使用せず）

**横断チェック結果**: `tests/`配下・ルート直下の全テストファイルを確認したところ、本番パスへの直接参照は`test_virtual_trader.py`のみで、他のテスト（`test_build_daily_report.py`・`test_build_public_report.py`・`test_csv_db_consistency.py`・`test_config_manager.py`・`test_market_snapshot_volatility.py`・`test_websocket_orderbook_depth.py`・`test_build_ai_review_summary_*.py`・`test_archive_old_logs.py`等）はいずれも`tmp_path`または引数経由の隔離パスを正しく使用していた。

**実行確認**: 修正後、pytest 186件すべてパス。`realtime_trading_log_2026-07-22.csv`のsha256・mtime_nsをpytest実行前後で比較し、完全に一致することを確認した。`live_state.db`はエンジン稼働中のため更新があったが、テスト起因ではないことを確認済み。

**教訓**: 仮想資金運用中は実害が限定的だが、テストが本番の状態ファイルに書き込める設計は、実資金移行後やVPS移行後にはより深刻な事故につながりうる。ダッシュボードの「総資産が減った」という体感（7.24参照）をきっかけに発覚したが、体感ベースの気づきに頼らず、`conftest.py`のような機械的なガードを常設したことで再発を防止できる状態にした。

### 7.24 2026-07-22: ダッシュボード「総資産」表示の混乱（計算バグではなくラベルの混在）

7.23の調査過程で「テスト後に総資産が減った」との申告があり、原因調査の結果、計算ロジック自体は正しく、表示・ラベルの混在が原因と判明。

**症状**: LONGポジション保有中にダッシュボードの数値を見て「総資産が減った」と感じたが、実際に見ていたのは`daily_history.jsonl`の`jpy_balance`（現金残高のみ）であり、ダッシュボードの「総資産」（`jpy_balance + 数量 × 現在価格`）とは別の値だった。LONGエントリー時は購入代金が現金から引かれBTCポジションに変わるため、現金残高だけを見ると資産が減ったように見えるが、BTC評価額を合わせた総資産としては変動していない（含み損益を除く）。

**原因**: ダッシュボードの「総資産」計算自体は元から正しかったが、`daily_history.jsonl`の`jpy_balance`（現金のみ）と、ダッシュボードの「総資産」（現金＋ポジション評価）という異なる指標に、明確な区別のないまま近い言葉が使われていたため、画面をまたいで混同が生じた。

**対応**:
1. 総資産計算式（LONG: `jpy_balance + size × mid`、SHORT: `jpy_balance + (entry − mid) × size`、FLAT: `jpy_balance`）を`btc_trading_tool/portfolio_metrics.py`として共通モジュール化し、`dashboard.py`と`build_daily_report.py`の両方から利用する形に統一（計算ロジックの重複・将来的な乖離を防止）
2. `daily_history.jsonl`に日末総資産`total_assets_eod`（05:55時点の`live_state.db`の現金・ポジション・板midから算出）を新規追加。既存の`jpy_balance`等のフィールドはそのまま維持
3. ラベルを整理: `dashboard.py`の仮想ポートフォリオ表示で「総資産」と「現金残高」を並列表示に変更。日次推移テーブルの列名を「資産残高」→「現金残高」に変更し、新たに「総資産」列（`total_assets_eod`ベース、追加前の日は空欄）を追加
4. `build_public_report.py`（4.6）・Telegram内部レポートは資産残高を表示していないため変更なし（スコープ外）

**テスト**: `tests/test_portfolio_metrics.py`（LONG/SHORT/FLATの計算）、`tests/test_build_daily_report.py`（LONG保有時の`total_assets_eod`記録）を新規追加。191件すべてパス。

**教訓**: 同じ「資産」という言葉でも、「現金のみ」「現金＋含み益込みの評価額」「確定損益のみ」という異なる指標が複数の画面・ファイルに分散していると誤解を招きやすい。計算ロジックが正しいことと、それが正しく伝わる表示になっていることは別問題であり、両方を意識する必要がある。

---

### 7.25 2026-08-06: `.gitignore`の`test_*`ルールがパス限定なしで`tests/`配下まで巻き込んでいた

Instagram/Threads自動投稿（4.7.7）の新規テストファイル（`tests/test_instagram_poster.py`等）を追加した際、`.gitignore`の`test_*`ルールにより`git add`しても追跡対象にならないことが判明。

**原因**: 2026-08-01のコミット（リポジトリルート直下に散らばっていた過去の一時/レガシーテストファイル19個を削除した際）に追加された`test_*`ルールが、`/test_*`のようなパス限定を付けていなかったため、本来の意図（ルート直下限定）を超えて`tests/`配下の正規テストファイル命名規則（`test_*.py`）まで再帰的にマッチしてしまっていた。既存の`tests/test_*.py`群は当時すでにgit追跡済みだったため影響が表面化せず、しばらく気づかれないままだった。

**対応**: `.gitignore`のルールを`/test_*`（リポジトリルート限定）に修正。副産物として、同じ理由で長期間追跡対象外のまま残っていた既存の実質的なテスト2件（`tests/test_account_integrity_baseline.py`・`tests/test_monitor_heartbeat_concurrency.py`）も発見し、追跡対象に追加した。

**教訓**: `.gitignore`のパターンはパス区切りの有無で影響範囲が大きく変わる（先頭に`/`が無いと全階層に再帰マッチする）。「追跡漏れのファイル」はローカルでは正常に動作して見えるため、`git status`やコミット内容を定期的に確認しないと気づきにくい。7.10（`runtime/`統合時の`.gitignore`パターン消失）と同種の失敗パターン。

### 7.26 2026-08-07: 日次メンテナンス時間帯のポジション強制決済が実装されていなかった（設計と実装の乖離）

### 7.27 2026-08-19〜21: order_rate_limit頻発→06:00再起動SKIP→trading_day_date固着→daily_history.jsonl重複ログ

**発端**: 2026-08-20 05:50:56、order_rate_limitサーキットブレーカーが発動し`manual_stop.flag`が作成された。約9分後の06:00、`engine-restart`はこのflagの存在によりSKIPされ（`restart_engine.sh`の既存仕様）、btc-engineコンテナは実際には約15時間後の2026-08-20T21:00:07（JST 8/21 06:00相当）まで再起動しなかった。

**表面化した事象**: 2026-08-21朝、Telegramで届いた日次レポートで、`daily_history.jsonl`の2026-08-19分に2行の重複（`realized_pnl`が-86.4円と-309.1円で不一致）があることに気づいた。

**調査結果**: `trading_day_date`（3.4.8）はエンジン起動時にしか更新されない設計のため、上記のrestart skipにより2026-08-19のまま約1日固定され、`build_daily_report.py`（毎日05:55実行）が8/20朝・8/21朝の両方で同一`trading_day=2026-08-19`として`daily_history.jsonl`に追記していた。1行目（8/20朝分）はCSV取引日ウィンドウの実集計と一致する正しい値、2行目（8/21朝分）は未リセットの`daily_realized_pnl`に8/20 06:00以降の損益まで混入した誤値だった。

あわせて、8/20は相場ボラティリティ急変により取引回数が平常時の約10倍に急増しており、これが冒頭のorder_rate_limit発動（およびその後8/20〜21で計31回の発動）そのものの主因であったことも判明した。

**対応**（詳細は各節参照）:
1. `daily_history.jsonl`の重複行削除＋`append_daily_history()`への重複追記防止ガード追加（3.4.52）
2. `trading_day_date`のロールオーバーを実時刻ベースに変更し、restart skip時も日次リセットが機能するよう恒久対応（3.4.53）
3. `ENTRY_COOLDOWN_AFTER_CANCEL_SEC`を5秒から10秒に延長し、order_rate_limit発動自体の頻度を抑制（3.4.54）

**未対応・保留事項**:
- 15時間停止そのものの間、なぜ`manual_stop.flag`が解除されずPAUSED状態が継続していたかの詳細な経緯は、`docker events`ログの保持期間切れにより完全な裏取りができず、根本原因の特定は保留とした（再発時に改めて調査）
- 長時間の停止・PAUSED状態を検知する監視（心拍監視は既存だが、PAUSED継続時間そのものへのアラートは未実装）は今回対応しておらず、9章に残課題として記録
- 同日調査したCSV/live_state.db整合性チェックの別事象（11:00, diff=+1）は、`FORCE_CLOSE_REAL`が`check_csv_db_consistency.py`の`EXIT_REASONS`に含まれていないことが原因と判明したが、今回は対応を見送った（9章参照）

05:01エントリーのLONGポジション（0.001 BTC）が、05:55〜06:30のメンテナンス時間帯に入っても強制決済されないまま残り続け、GMO側にも建玉が残っていることが判明した。エンジンはRUNNING状態のまま稼働しており、クラッシュや停止は発生していなかった。手動で成行決済して対応した。

**調査結果**: 仕様書（3.2節）には「保有中ポジションは強制決済（`FORCE_CLOSE_MAINTENANCE`）」と記載されていたが、実際のコードでは日次メンテナンス窓（`in_daily`）の経路は`_is_entry_blocked()`による新規エントリー停止と状態遷移ログの出力のみで、`_force_close_real()`（強制決済の実処理）を呼び出す分岐が存在しなかった。強制決済が実際に実行されるのは、(1) `manual_stop.flag`検知時、(2) 週次メンテナンスのプレメンテ枠（`in_pre`）かつ`maintenance_pre_action=close`設定時、の2経路のみだった。バグ（判定ミスや例外による握りつぶし）ではなく、そもそも実装当初から日次窓には強制決済への接続が存在しない、という設計と仕様書記載の乖離だった。06:00の`engine-restart.timer`によるエンジン再起動もメンテナンス中に発生していたが、これは無関係（再起動後もポジションは正しく復元され、同じ「強制決済が呼ばれない」状態が続いただけ）。

**対応方針の検討**: 仕様書記載通りに日次窓でも強制決済を実装するか、現状の実装（エントリー禁止のみ）に仕様書を合わせるかを検討した。GMOコインのレバレッジ取引は取引手数料自体が実質無料（Maker/Taker共に0%）で、決済コスト（スプレッド・スリッページ）は強制決済しても持ち越して本来のTP/SLで決済しても同じ種類のコストが「今か後か」で発生するだけのため実質差がない。強制決済でのみ回避できるのは建玉評価額に対する0.04%/日のレバレッジ手数料のみで、ポジション規模が大きくなっても％としては変わらず、絶対額も小さい（例: 0.001BTC・約1万円分で約4円/日）。また日次メンテナンス時間帯はGMOの取引所自体が完全停止するため、持ち越している間の価格変動リスクも実質発生しない。一方、強制決済は本来のTP/SLに届く前に人為的に途中終了させることになり、決済価格が不利になるリスクの方が金額的に効いてきやすいと判断した。

**決定**: 日次メンテナンス時間帯（05:55〜06:30）は現状の実装（新規エントリー停止のみ、既存ポジションは持ち越し）を維持する方針とした。仕様書側を実態に合わせて修正（3.2節）。週次メンテナンスのプレメンテ枠およびmanual_stop.flag経由の強制決済は従来通り維持。

---

## 8. 定期メンテナンス（月次目安）

`ai_review/README.md`に詳細なチェックリストがあるが、要点は以下の通り。

1. **絶対範囲（PARAMETER_ABSOLUTE_BOUNDS）の妥当性**: `clamped_to_bounds_fields`の頻発を確認し、狭すぎ/広すぎを判断
2. **`held_profile_name_mismatch`の傾向**: AIが繰り返しプロファイル構成の見直しを提案していないか
3. **`ai_validation_failures.jsonl`の傾向**: 同じエラーパターンの繰り返しがないか
4. **時間帯プロファイルそのものの妥当性**: `early_morning`/`daytime`/`night`の区切りが実際の成績差を反映しているか、ログを見て再検証する
5. **無料枠の使用量**: Groq/Geminiのダッシュボードで確認
6. **ログ肥大化・ディスク容量**: 保持期間・アーカイブ方針の確認
7. **仕様書の記述と実装の突き合わせ**: `config.json`のサンプルJSON（2章）や
   各種判定ロジックの説明（3.2章のメンテナンス判定等）が、実装の最新状態と
   食い違っていないか確認する。特に「仕様書には書かれているが実装されて
   いない」パターン（7.5のインシデント参照）は通常の動作テストでは気づき
   にくいため、`grep`等で仕様書中のパラメータ名・定数名を実コード上で
   検索し、対応する実装が存在するかを機械的に突き合わせることを推奨する

---

## 9. 未実装・今後の課題

- ~~`gmo_fetch_order_execution_fill`連続失敗時のアラート（詳細は3.4.48）~~ → **完了（2026-08-07）**。2026-08-04〜05のTP実約定価格取得バグ（APIキー権限不備によるERR-5012）を踏まえ、連続失敗が3回に達するたび（3/6/9…、一度で沈黙しない）にTelegramへCRITICALアラートする仕組みを実装・VPS反映済み。
- **Docker向けAI夜間レビュー手動起動コマンドの運用ドキュメント化（低優先度、詳細は3.4.28）**: `_launch_nightly_review()`のDocker/Linux対応（3.4.28）に伴い、非Windows環境では画面上で「ai_reviewコンテナ側で別途実行してください」と案内するようにしたが、対応する`docker compose run --rm ai_review ...`相当の具体的な手順はまだ運用マニュアルに書いていない。画面メッセージと実際の手順を揃えるため、VPS移行時までに追記する。
- **Xへの公開レポート自動投稿（フェーズ1完了、フェーズ2以降は未着手、詳細は4.6）**: 2026-07-22、日次・テキストのみのフェーズ1（`build_public_report.py`、許可リスト9項目の集計・X API投稿）を実装し、実機での初回投稿に成功した。同日、許可リストを累積収益率(%)から累積損益(円)・当日損益(円)・当日増減率(%)へ見直し、あわせて当日損益のデータソースを（06:00リセットの影響を受ける）`live_state.db`から`daily_history.jsonl`のスナップショットへ修正済み（4.6.1）。VPS移行後は`public-report.timer`としてsystemd配備・稼働中（4.5.11、投稿時刻は2026-08-07に07:30へ変更）。以下は未着手:
  - ~~`BTC_Public_Report_Post`タスクの本登録（Windows Task Scheduler）~~ → VPS移行に伴い`public-report.timer`（systemd）へ移行済み。現在は手動実行での試験運用ではなく自動配信が稼働中
  - フェーズ2: 累積損益の推移グラフ画像（matplotlib等）を投稿に添付
  - フェーズ3: 週次・月次レポートの自動集計・自動投稿（`build_ai_review_summary.py`の14/30/90日ウィンドウ集計の考え方を流用予定）
  - ~~勝率の公開種別（累積 or 当日）は現状`PUBLISHED_WIN_RATE`定数で累積を既定としているが、運用しながら要否を再検討してもよい~~ → **解消（2026-07-23）**。累積勝率・当日勝率の両方を常時出力する形に変更した（4.6.1参照）
  - `INITIAL_JPY=50000`が複数ファイルにハードコードされている点は、初期資産を変更するタイミング（実資金移行等）で一元化を検討する（急ぎではない）
  - `runtime/manual_stop_reason.json`は最新1件のみ保持のため、サーキットブレーカーが対象日以外に発動し未解除のまま日をまたいだ場合の判定は単純な実装のまま。実際にそのケースが発生してから、発動履歴のjsonl化等の厳密化を検討する
- **3.4.14関連の残タスク**（ポジション未復元バグ修正、2026-07-23）:
  - ~~日次メンテナンス前の強制決済（土曜と同じ仕様）の追加は意図的に見送り~~ → 2026-08-07、この論点自体を7.26で再検討し、現状維持（強制決済を実装しない）の方針を正式決定。詳細は3.2・7.26参照
  - real mode起動時のGMO取引所側の建玉との突合の必須化（3.4.12参照、real mode移行時に対応）
- **4.7 SNS拡張（Instagram/Threads向け）の残タスク**（2026-08-06時点、詳細は4.7・4.7.7）:
  - ~~ステップ2: 動画（.mp4）生成の実装~~ → **完了（2026-07-24）**
  - ~~Instagram/Threadsへの自動投稿（手動オペレーションからGraph API経由への切り替え）~~ → **完了（2026-08-06）**。詳細は4.7.7参照
  - ~~Instagramのアカウント種別によるBGM制限の懸念~~ → **解消**。Geminiで作成した自前のオリジナル楽曲を使う方式にしたため、Instagram側の音源ライブラリ制限自体が関係なくなった（4.7.7参照）
  - トークン有効期限の厳密な確認（`refresh_sns_tokens.py --check-only`）には、現状`.env`に持っていないMeta App DashboardのApp ID/App Secret（`app_id|app_secret`形式のアプリアクセストークン）が必要。無くても週次自動リフレッシュ自体には支障ないため優先度は低いが、必要になれば追加する
  - 全期間の累積損益推移グラフ＋固定ポスト運用は、運用開始から最低2ヶ月経過後に検討する（低優先度）
- **3.4.15関連の残タスク**（エンジン完全停止検知、2026-07-23）: `check_engine_crash_loop.py`（Docker向け）・`BTC_Detect_Orphan_Engines`（多重起動検知）・`check_engine_process.py`（完全停止検知、今回追加）の3つの監視スクリプトの役割が重複・混同しやすいため、VPS/Linux移行のタイミングでドキュメントとして改めて整理する
- **通知システムの拡張**: AI議論パイプラインの失敗通知（5.7）・異常検知アラート（取引頻度・損益急変の1時間毎監視、`scripts/check_trading_anomaly.py`）は実装済み（後者は9章に長らく「未実装」と誤記されていたことが2026-07-12に判明、7.8参照）。**タスクスケジューラ登録は2026-07-12に完了**（`BTC_Anomaly_Check`、4.3参照）。登録にあたり、既存の`scripts/run_anomaly_check.ps1`（2026-07-10の初回コミット時点で作成済み・未登録のまま放置されていたもの）をCursorに再整備させ、`ai_review/run_nightly_review.ps1`と同じ設計（ロックファイル・ログ出力・try/catch/finally）に揃えた。**登録直後の初回実行で、エンジン再起動によるカウンターリセットを誤検知しうる問題が発覚し修正済み（7.11参照）**。
  - ~~日次レポート（決済件数・資産増減・KPIを1日1回Telegramへ）~~ → **完了（2026-07-17）**。`scripts/build_daily_report.py`／`scripts/run_daily_report.ps1`として実装し、`BTC_Daily_Report`として毎日05:55登録・実機でTelegram通知を確認済み（詳細は4.1・4.3参照）。集計対象は決済件数・勝率・実現損益の最小構成（資産増減の細かいKPIは対象外）。実装過程で、Python側に既存パターンにない独自ロック機構（`_acquire_lock`/`_release_lock`＋環境変数によるバイパス）が一度混入したが、`check_csv_db_consistency.py`と同じ「ロックはPS1側のみ」という既存方針に揃えて簡素化した
- **サーキットブレーカー（実装済み、詳細は3.4）**: 「取引頻度（1分あたり発注回数）が異常値を超えたら金額に関わらず自動停止」は実装済み（2026-07-10）。「1日の累積損益が開始時残高の一定割合を超えて悪化したら停止する」仕組みも**2026-07-14に実装完了**（3.4.8参照、閾値は開始時残高比10%、実現損益ベース、`manual_stop.flag`による緊急停止）
- **CSVとlive_state.dbの整合性チェック（実装済み・タスクスケジューラ登録済み、詳細は3.4.9）**: 「その日のCSVの決済件数」と「live_state.dbのカウンター増分」を定期的に突き合わせ、乖離していたら警告を出す仕組み。2026-07-14実装、同日`BTC_CSV_Consistency_Check`として毎時登録し実機初回実行で動作確認済み
- ~~AIが提案した変更のバックテスト検証~~ → **完了（2026-07-17）**。`market_snapshot`過去30日分を使い、`imbalance_entry_threshold`/`take_profit_pct`/`stop_loss_pct`の変更提案を変更前後の値でそれぞれ簡易シミュレーションし、悪化判定なら適用しない仕組みを実装（詳細は5.4.3）。厳密なウォークフォワード検証ではなく、60秒間隔の`market_snapshot`データによる簡易版である点に注意
- **AI自動更新の承認方式について（検討経緯）**: 当初は人間による事前承認（Telegram等）も検討したが、03:30開始・06:00反映という深夜〜早朝サイクルとの相性が悪く（早朝の手動承認は運用上厳しい、平常時が続くと承認が形骸化する）、5.4.2の外れ値検知＋段階適用（統計的異常検知による自動足止め）に方式を切り替えた。人間の事前承認は挟まず、Telegramは事後報告のみとする方針で決定済み
- ~~`pending_rollouts`のメタデータとconfig実値の食い違い~~ → **完了（2026-07-15）**。詳細は5.4.2参照
- **Git管理によるコードのバージョン管理（実装済み、詳細は4.4）**: 2026-07-10、`gmo-btc-ws`をGitリポジトリ化し、GitHub（`https://github.com/festallypicco/gmo-btc-ws`、Private）へ初回pushまで完了。`config_history.jsonl`が設定差分を記録するのに対し、こちらはコード自体（`virtual_trader.py`等）の変更履歴を管理する。ローカル⇔VPS間のpush/pullによるデプロイフロー運用は、VPS移行時に本格運用開始予定（現時点はローカル内でのclone確認のみ実施済み）
- ~~ログの自動アーカイブ~~ → **完了（2026-07-17）**。`scripts/archive_old_logs.py`／`scripts/run_archive_old_logs.ps1`として実装し、`BTC_Log_Archive`として毎日04:30登録・実機で正常終了（対象月なし）を確認済み。保持期間は全ログ種別一律90日（`build_ai_review_summary.py`の`regime_reference`が90日分の生CSV（`realtime_trading_log_*`・`market_snapshot_*`）を直接読みに行くため、90日未満への短縮は不可という制約を確認した上で決定）。90日より古い「まるまる1ヶ月分」を月単位でzip化し元ファイルを削除する。`config_history.jsonl`等の日付を含まない追記型ファイルは対象外。zip検証（`testzip()`）通過後にのみ元ファイルを削除する順序を厳守
- ~~`run_daily_report.ps1`のログ二重記録~~ → **完了（2026-07-17）**。`build_daily_report.py`が`logging.FileHandler`で`log/daily_report_YYYY-MM-DD.log`に直接書き込むと同時に、`run_daily_report.ps1`の`Invoke-PythonScriptAndLog`が標準出力（`StreamHandler`経由）を同じファイルにAdd-Content追記しており、同一ログ行が2回記録されていたことを確認。`archive_old_logs.py`実装時（2026-07-17）に`run_archive_old_logs.ps1`側でラッパー専用のログファイル名に分離する対応を先に行ったことで気づいた。対応として`build_daily_report.py`の`_setup_logging()`から`FileHandler`を削除し、`StreamHandler(sys.stdout)`のみに変更（ログファイルへの書き込みはPS1側のAdd-Contentに一本化）。ログファイルへの書き込みを一切行わない構成のPythonスクリプト（`check_monitor_heartbeats.py`等、PS1側が標準出力をキャプチャする方式）と揃えた形。次回05:55の実機実行後、`daily_report_*.log`が1行ずつの記録になっているか確認予定
- **ログフォルダの種類別整理**（緊急性低）: `log/`直下がフラットな構成のため、将来的に`trading/`・`market/`・`engine/`・`ai_review/`のようにサブフォルダで整理する案がある。処理速度への影響は無視できる水準だが、AIへの作業依頼時の見通しやすさ・アーカイブ方針の管理しやすさの観点でメリットがある。既存コード側のパス参照箇所が多いため、実施する場合は土曜メンテナンス時間帯（09:00-11:00）に作業し、作業直後に手動でエンジンを再起動して新しいパスを反映させること
- **Ubuntu VPS移行 / Docker化（ローカル運用実績の蓄積待ち、2026-07-20以降にVPS契約を検討。詳細は4.5）**: 現在Windows11ローカルPCで運用中。2026-07-12時点で以下2点は完了。
  1. ~~`manual_stop.flag`・`live_state.db`・`trading_engine.pid`の参照箇所の洗い出し~~ → 完了（4.5.1参照）
  2. ~~案Bの実装（`runtime/`フォルダ新設＋パス定数の一括修正、docker-compose.test.ymlの修正、本番用docker-compose.ymlの新規作成、systemdタイマー定義）~~ → 完了（4.5.1〜4.5.4参照）

  残タスクは以下の通り（4.5.5「未検証の範囲」も参照）。
  1. ~~`ai_review/run_nightly_review.sh`の構文検証~~ → 完了（2026-07-12）。`bash -n`で15行目`log() {`にCRLF（`\r`）混入によるsyntax errorを検出。`tr -d '\r'`で除去して解消。再発防止として`.gitattributes`に`*.sh text eol=lf`を追加済み
  2. ~~Dockerfileのビルド確認~~ → 完了（2026-07-12）。Docker Desktop（WSL2バックエンド）にて`docker build --no-cache`でクリーンビルドが成功（`pip install`含め約43秒）。`python -c`による`trading_engine`・`btc_trading_tool.dashboard`・`ai_review.review_pipeline`のimport確認も実施し、エラーなし（`dashboard`側の大量warningは`streamlit run`を経由しない実行方法によるstreamlit側の想定内メッセージ）
  3. ~~ローカルでのDocker統合テスト~~ → **完了（2026-07-14）**。`docker-compose.test.yml`で`engine`/`dashboard`間の`runtime/`共有（`live_state.db`・`config_version`・`manual_stop.flag`）、ログ共有、`manual_stop.flag`検知からの安全停止を実機確認した。この過程で発見した`on-failure`無制限リトライ問題への対応（`on-failure:5`＋クラッシュループ検知・通知）も完了（4.5.6・7.12参照）
  4. ~~**VPS実機の構築**~~ → **完了（2026-08-01、ConoHa VPS）**。Docker Engineが入ったUbuntu 24.04サーバーを構築し、Gitでclone。`deploy/systemd/`配下の9セット（`ai-review.*`・`engine-restart.*`・`crash-loop-check.*`・`anomaly-check.*`・`csv-consistency-check.*`・`monitor-heartbeat-check.*`・`orphan-orders-check.*`・`public-report.*`・`sns-report.*`）を配備し、`WorkingDirectory=/opt/gmo-btc-ws`で`enable --now`まで完了。実機構築時に発生した多数のつまずき（ConoHaのセキュリティグループ未割当、Ubuntu 24.04のsocket activationによるSSHポート変更未反映、GitHub Deploy Key、Docker権限の再ログイン要件、venv構築順序の章立てズレ等）を反映し、`VPS構築手順書.md`をv11として全面刷新した
  4-2. ~~**（2026-07-30判明）`run_public_report.sh`・`run_sns_report.sh`のPython参照方式の確認**~~ → **完了（2026-07-31、詳細は3.4.38）**。修正の過程で4.5.8の3スクリプトも実際にはvenvを参照していなかったことが判明し、監視・レポート系5本すべてを`.venv-host`明示参照に統一した
  4-3. ~~**（2026-08-01判明）`run_crash_loop_check.sh`・`run_orphan_orders_check.sh`のPython参照方式の確認**~~ → **完了（2026-08-01）**。4-2の5本修正の対象に含まれておらず、この2本だけ`python`コマンドを裸で（フルパス指定なしで）呼んでいる箇所が残っていたため、VPS実機での本番稼働開始直後にsystemdタイマー実行が`python: command not found`（exit=127）で失敗した。両ファイルとも既存の`PROJECT_ROOT`変数を使い`"$PROJECT_ROOT/.venv-host/bin/python"`への絶対パス化で解消（VPS側・リポジトリ側の両方に反映済み）。教訓として、host側で動くスクリプトを追加・変更するたびに裸の`python`呼び出しが残っていないか確認する運用ルールを`VPS構築手順書.md`v11の3.6節・8.10節に明記した
     **契約タイミング（2026-07-14決定）**: ローカルでの運用実績を最低2週間分（クラッシュループ対策・統合テスト後の状態で）確認してから契約する方針。7/3〜7/5はたたき台状態での運用だったため実質的な検証期間としてカウントせず、**最短でも2026-07-20以降**にVPS契約を検討する（7/6以降を実質の起点とし、そこから2週間弱）。それまでは実機なしで進められる準備作業（VPS構築手順書の整理、`deploy/systemd/`配下ファイルの事前チェック等）を進める → **2026-08-01にConoHaで契約・構築完了**
  5. ~~**ネイティブ→Docker本番切り替え**~~ → **完了（2026-08-01）**。土曜09:00〜11:00のメンテナンス枠で、タスクスケジューラ無効化（5種類）→ネイティブエンジン停止確認→real-test環境が起動していないことの確認→コンテナ起動→二重稼働していないことの確認→systemdタイマー9セット有効化、の順で1回のみ実施した
  6. ~~**SSHトンネル経由でのダッシュボードアクセスの実地確認**~~ → **完了（2026-08-01）**（4.5.3で方式決定済み）
  7. ダッシュボードの外部公開は行わない方針のため認証追加は現時点では不要（4.5.3参照。将来VPN等で多人数アクセスに広げる場合は再検討）
  8. **（低優先度）テスト用compose（`docker-compose.test.yml`）自体のクラッシュループ監視**: 現状`BTC_Crash_Loop_Check`は本番`docker-compose.yml`のみを対象としている。ローカルテスト中は手動で気づける範囲のため優先度は低いが、対応する場合は別タスク登録または`run_crash_loop_check.ps1`への引数追加が必要（4.5.6参照）
  9. ~~`engine`サービスの06:00強制再起動に相当する仕組みの追加~~ → **完了（2026-07-14）**。`scripts/restart_engine.sh`＋`deploy/systemd/engine-restart.timer`/`.service`を新規追加（4.5.7参照）。VPS実機での有効化も**完了（2026-08-01）**
- ~~`scripts/reset_trading_state.py`の新カラム未対応~~ → **完了（2026-07-15）**。詳細は3.4.8参照
- ~~`.env`ファイルの重複~~ → **完了（2026-07-15）**。詳細は5.6参照
- ~~監視系スクリプト自体の死活監視~~ → **完了（2026-07-15）**。`check_monitor_heartbeats.py`（`runtime/monitor_heartbeats.json`のハートビート監視、`BTC_Monitor_Heartbeat_Check`として15分毎登録）と、3つの`.ps1`ラッパーのcatchブロックへの即時Telegram通知を実装した（詳細は4.1・4.3）
- **Proposer向けプロンプトの安全弁（step_b）が常態化していないかの経過観察（2026-07-17判明、詳細は7.7）**: 3回目の413エラー対応として段階的安全弁（step_a/step_b）を実装したが、実測ではstep_a・step_bの両方が発動して初めて安全ラインに収まった。`[WARN]`ログの発生頻度を継続的に見て、毎回step_bまで発動する（＝`rule_review`の`market_*`が常にProposerから除外される）状態が続くようなら、3窓構造そのものの見直し等、別途の根本対応を検討する
- **実資金移行（フェーズ1＝発注ロジック本体は完了、フェーズ2＝隔離環境疎通確認は稼働継続中、詳細は3.4.10〜3.4.19）**: `trading_mode`フラグ・Private WebSocket接続基盤・`manual_stop.flag`のreal mode版強制決済`_force_close_real()`（いずれも2026-07-15）に続き、当初想定より1週間以上ボリュームが大きいことが判明したため計画を日付固定から完了条件ベースのフェーズ制（フェーズ1: 発注ロジック実装 → フェーズ2: 隔離環境疎通確認 → フェーズ3: 少額実弾スモークテスト → フェーズ4: 本格運用）へ見直した。フェーズ1は以下をすべて実装完了:
  - ~~`VirtualTrader`との連携（ENTRY約定検知、TP用LIMIT・SL用STOPの同時発注、合成OCO＝どちらか約定時のもう片方キャンセル）~~ → **完了**。ENTRY発注（指値Maker、`SOK`）とPrivate WS約定検知に加え、キャンセル経路の安全化（imbalance反転・メンテナンス強制キャンセルの両方で、内部状態を戻す前に必ずGMOへ実際のキャンセルAPIを送るよう修正。キャンセル不可＝既に約定済みの場合は建玉を保有中として採用）を実装。約定後はTP（LIMIT/`SOK`）・SL（STOP/`FAK`、いずれもGMO公式のtimeInForce制約に準拠）を同時発注し、合成OCO（片方約定でもう片方を自動キャンセル、キャンセル不可＝想定外の二重約定時は緊急通知）で決済する。あわせて、板監視ベースの決済判定（`_check_active_position`）がreal modeでも動作し続けており、GMO側の実約定を待たずにローカルで二重決済してしまう不具合を発見し、real modeでは板ベース決済を完全にスキップするガードを追加して解消した
  - ~~再起動時の状態復元（GMOの建玉・有効注文からの再構築。建玉あり・TP/SL不一致時は自動復旧を試み、同一建玉で2回目が発生した場合は自動復旧せず安全停止に切り替える設計は決定済み・未実装）~~ → **完了**。`entry_order_id`・`tp_order_id`・`sl_order_id`をlive_state.dbへ永続化・復元する処理に加え、起動時（real modeのみ）にGMOの実際の建玉・有効注文一覧と突き合わせる`reconcile_real_state_on_startup()`を実装。エントリー未約定のまま停止していた場合の建玉採用・拘束資金返却、保有中ポジションでTP/SLいずれかが停止中に消失していた場合の欠落側のみ再発注、対応する建玉自体が消失していた場合のクリア＋緊急通知まで対応。同一ポジションで2回連続してズレが解消しない場合は、再発注せず`manual_stop.flag`による安全停止に切り替える（設計通り）
  - ~~3.4.2で指摘済みの、発注サイズ計算基準を実口座残高ベースへ切り替える設計変更（real mode時のみ）~~ → **完了**。`_calc_trade_size`がreal mode時は内部帳簿（`self.jpy_balance`）ではなく`fetch_real_account_state`で取得した実口座残高を基準に計算するよう変更（取得失敗時はエントリーを見送り、内部帳簿は今まで通り決済時の反映等に使用）
  - ~~発注権限付きAPIキーの新規発行、隔離テスト環境（`docker-compose.real-test.yml`想定）での疎通・少額実弾確認~~ → **APIキー発行分は完了**。参照専用キーとは別に、資産残高取得・注文・決済注文・注文キャンセル・有効注文一覧・建玉一覧取得・約定情報通知(WebSocket)・注文情報通知(WebSocket)の8権限のみに絞った新規キーを取得（出金・注文変更・ロスカットレート変更等は付与せず）。これに伴い、GMO Private APIの認証情報を`GMO_API_KEY_TRADE`/`GMO_API_SECRET_TRADE`（発注系、trading_engine.pyが使用）と`GMO_API_KEY_READONLY`/`GMO_API_SECRET_READONLY`（参照専用、孤児注文チェック等が使用）に分離し、Private WebSocket（`websocket_manager.py`）側の認証情報取得も新しい変数名（`GMO_API_KEY_TRADE`系）に統一。`.env`から旧`GMO_API_KEY`/`GMO_API_SECRET`は削除済み。**隔離テスト環境（`docker-compose.real-test.yml`）での疎通確認はフェーズ2として2026-07-26に着手し、稼働継続中**（発見・修正した不具合の詳細は3.4.16〜3.4.19参照）
  - ~~3.4.2の拡張（有効注文一覧の孤児注文チェック）~~ → **完了**。`scripts/check_orphan_orders.py`（`GMO_API_KEY_READONLY`系を使用）を新規追加し、`BTC_Orphan_Orders_Check`として5分間隔でTask Scheduler登録。GMOの有効注文一覧のうち、live_state.dbの`entry_order_id`/`tp_order_id`/`sl_order_id`いずれとも一致しないものを孤児注文としてTelegramでアラート（自動キャンセルはしない）。VPS/Linux向けsystemd配備・死活監視への組み込みは2026-07-28に完了（3.4.25・4.5.10）
  - **フェーズ2（隔離環境疎通確認、2026-07-26着手・稼働継続中）**: 認証・シンボルまわりの起動不能解消（3.4.16）、決済方式の抜本的な設計変更（反対売買ではなく`closeOrder`が必須と判明、3.4.17）、内部残高会計のLONG/SHORT非対称の修正（3.4.18）、実口座残高参照先の整理（3.4.19）を実施済み。保有中の再起動・意図的なキャンセル等、これまで見つかったバグの経路を意識的に踏んで不具合を洗い出す方針で継続中
  - **緊急停止時の孤児注文発生とCursorレビュー対応（2026-07-27〜28）**: 隔離環境で緊急停止を実行した際、エントリー中の指値注文がGMO側に取り消されずに残る実害が発生した（画面から手動で取り下げ）。これを機にCursorへコードレビューを依頼し、Critical〜Lowで分類された指摘を優先度順に対応した: 保有中口座照合の誤検知修正（3.4.20）、今回の実害そのものである緊急停止時pendingエントリー未キャンセルの修正（3.4.21）、発注用APIキーの多重起動排他制御（3.4.22）、SL約定後・強制決済後のequity/KPI同期（3.4.23）、`repair_live_state_equity.py`のreal mode対応（3.4.24）、孤児注文監視の本番配備・死活監視組み込み（3.4.25）
  - 残タスク:
    - ~~`ERR-5122`/`ERR-5123`が実際のエラーレスポンスで想定どおりの`message_code`として返ってくるかの実機検証~~ → **完了（2026-07-29）**。`_force_cancel_maintenance()`側・`_force_close_real()`側ともに実機確認済み（詳細は3.4.21・3.4.29）。ただし`_force_close_real()`側は、ログ文言からエラー検知経路そのものの厳密な確証までは取れていない（挙動としての安全性は確認済み）
    - **エンジンクラッシュの原因調査**: 2026-07-26〜27のフェーズ2作業中、07:09:42前後でreal-testコンテナが原因不明で停止した事象があったが、`docker compose down`によりログが失われ特定できなかった（4.5.9で運用対策済み）。今後同様の事象があればログを保全してから調査する
    - ~~**SHORT側の一連の流れの確認**~~ → **完了（2026-07-29）**。詳細は3.4.29
    - ~~**両compose同時起動時の二重発注防止（3.4.22の既知の制約、2026-07-30方針決定）**~~ → **反映済み（2026-08-01）**。`docker-compose.yml`と`docker-compose.real-test.yml`はruntimeパスが別のため、`gmo_trade_key.lock`だけでは両者の同時起動を防げない。VPS移行後はreal-testを本番と並行運用しない方針としたため（9章「VPS移行後の並行運用方針」参照）、`gmo_trade_key.lock`側でのコード対応（クロスホスト対応）は不要と判断。本番切替手順（`VPS構築手順書.md`v11 7.2節手順3）に、切替前にreal-test環境が停止していることを確認する運用手順を追記済み
    - **本番切替時、`GMO_API_KEY_READONLY`が仮想資金（デモ）用キーのまま残っていた事象（2026-08-01発生・解消）**: VPS本番稼働開始・`trading_mode=real`切替直後、`orphan-orders-check.timer`（5分毎）が`ERR-5012`（Invalid API-KEY, IP, or permissions）で失敗し続ける事象が発生。原因は`.env`の`GMO_API_KEY_READONLY`／`GMO_API_SECRET_READONLY`に、GMOコイン管理画面上「BTC自動売買デモ」ラベルのキー（仮想資金用）が設定されたままだったこと。**`check_orphan_orders.py`は`trading_mode≠real`の場合はGMOへのAPI呼び出し自体をスキップする設計のため、ネイティブPCでの`virtual`運用中はこの設定ミスが一度も表面化していなかった**。GMOコインで「BTC自動売買本番用」口座に対応する参照専用キー（有効注文一覧・建玉一覧・資産残高等の照会権限のみ、発注・キャンセル・出金権限なし）を新規発行し`.env`を差し替えて解消。教訓として、`trading_mode`をvirtual→realに切り替える際は、`config.json`の値だけでなく`GMO_API_KEY_READONLY`系がどの口座に対応するキーかも必ず照合する運用チェックを`VPS構築手順書.md`v11 7.0節・8.14節に追加した
    - **孤児注文監視の心拍が実質未検査状態でも正常に見える問題（3.4.25の既知の制約）**: `check_orphan_orders.py`の`load_trading_mode()`はconfig.json読み取り失敗時も`virtual`扱いでスキップし、その場合も心拍が更新されるため、real mode運用中にconfig破損等でチェックが実質機能していなくても死活監視上は「正常」に見えてしまう
    - **adopt後の同一tick内での連続close未対応（3.4.21の提案のみ事項）**: pendingエントリーがERR-5122等で建玉採用（adopt）された場合、その場では`_force_close_real()`を呼ばず次回の板更新まで待つ設計。Private WSが不安定な場面では建玉が生きたまま放置されるリスクが残る
    - フェーズ2完了後のフェーズ3: 少額実弾でのスモークテスト
    - （低優先度）AI夜間レビューが将来Moderator出力を丸ごとマージする実装に変わった場合に備えた、トップレベルキーのallowlist化
- ~~Windows再起動後、エンジンが自動復帰しない~~ → **完了（2026-07-15）**。2026-07-15にWindowsの意図しない再起動でエンジン・ダッシュボードともに停止し、次の06:00まで自動復帰しない状態が実際に発生したことを受け、「Windows起動時」トリガー（2分遅延）で`scripts/ensure_engine_running.ps1`を実行する`BTC_Engine_AutoStart_OnBoot`タスクをタスクスケジューラへ追加した（GUI登録、詳細は4.3）。二重起動防止は`ensure_engine_running.ps1`自体の既存ロジック（全プロセススキャンで1件でもあればスキップ）に委ねており、新規のロック機構は追加していない。ダッシュボード（Streamlit）の自動起動は対象外で、引き続き手動（`run_dashboard.bat`）
- ~~`run_dashboard.bat`実行時に`ensure_engine_running.ps1`が`Resolve-Path`エラーで失敗~~ → **完了（2026-07-15）**。原因は`run_dashboard.bat`側。`%~dp0`は末尾に`\`が付くため、`-ProjectRoot "%ROOT%" -StartupTimeoutSec 15`という呼び出しの実体が`-ProjectRoot "C:\...\gmo-btc-ws\" -StartupTimeoutSec 15`となり、末尾の`\`が直後の閉じ`"`をエスケープしてしまい、`-ProjectRoot`の値に`-StartupTimeoutSec 15`まで連結された文字列が渡ってしまっていた（典型的なバッチファイルの罠）。`run_dashboard.bat`で`ROOT`変数の末尾`\`を除去してから渡すよう修正（`ensure_engine_running.ps1`側の`param()`定義自体に問題は無かった）。実機確認済み: `Resolve-Path`エラーなし、二重起動防止ログ（`existing trading_engine process is running; skipped startup`）確認、Streamlit起動確認
- ~~`.ps1`ラッパーがPythonの`sys.exit(1)`（正常終了パスとしての異常系）を検知できない~~ → **完了（2026-07-15）**。4本（`run_anomaly_check.ps1`・`run_csv_consistency_check.ps1`・`run_crash_loop_check.ps1`・`run_check_monitor_heartbeats.ps1`）それぞれについて、Python側が非0終了コードで終わった分岐に`Send-WrapperTelegramAlert`（catchブロックと共通の通知関数）の呼び出しを追加した。通知本文フォーマットは`[ALERT] wrapper exception / script=<ラッパー名> / error=<詳細>`で共通化し、`error=`にPythonスクリプト名と終了コード（例: `check_trading_anomaly.py exit=1`）を含める。`run_check_monitor_heartbeats.ps1`は`Send-WrapperTelegramAlert`自体が未定義だったため、他3本と同形式で新規追加した。ロック鮮度によるスキップ（Python未実行のまま`$exitCode=1`になるケース）は今回の通知対象から意図的に除外している（正常なスキップを異常として誤通知しないため）。`run_check_monitor_heartbeats.ps1`のcatchブロック（PowerShell側の例外）へのTelegram通知追加も**完了（2026-07-17）**。他3本と同じ形式（`Send-WrapperTelegramAlert`をcatchブロック内で呼び出す）に揃え、4本すべてでcatchブロック・非0終了コード分岐の両方から通知される状態になった
- ~~約定データ（trades）のAI夜間議論への組み込み~~ → **完了（2026-07-17）**。`market_trades`集計ブロック（買い/売り出来高合計・買い比率・平均約定件数・`trades_actual_days`）を14日/30日/90日の各ウィンドウに追加し、3役とも集計値のみを渡す形で実装した（詳細は5.2.1・5.3）。旧ログ（trades列なしの日）は0扱いせず集計から除外し、`trades_confidence`という独立した信頼度ラベルで充足度を表現する。`review_pipeline.py`側の安全装置（外れ値検知・段階適用・バックテスト検証ゲート等）・Moderator出力フォーマットには変更なし。`tests/test_build_ai_review_summary_trades.py`で欠損日パターンを含め確認済み
- ~~板の複数階層の厚み（depth5）のAI夜間議論への組み込み~~ → **完了（2026-07-17）**。`market_trades`と同じ設計で`market_depth`集計ブロック（`bid_depth5_size`/`ask_depth5_size`/`depth_imbalance`の平均・`depth_actual_days`・`depth_confidence`）を14日/30日/90日の各ウィンドウに追加した（詳細は5.2.2・5.3）。旧ログ（depth5列なしの日）は0扱いせず集計から除外。3役とも集計値のみを渡し、Moderator出力スキーマ・安全装置には変更なし。9件のテストで欠損日パターン・Proposerプロンプト確認・既存trades回帰を確認済み
- ~~ボラティリティ（volatility_5min_range_pct）のAI夜間議論への組み込み~~ → **完了（2026-07-17）**。`market_trades`/`market_depth`と同じ設計で`market_volatility`集計ブロック（`volatility_5min_range_pct`の平均・最大値・`volatility_actual_days`・`volatility_confidence`）を14日/30日/90日の各ウィンドウに追加した（詳細は5.2.3・5.3）。列が無い旧ログに加え、列はあるが値がnullの行（エンジン起動直後のバッファ不足）も0扱いせず除外。3役とも集計値のみを渡し、Moderator出力スキーマ・安全装置には変更なし
- ~~**手数料・スリッページの実測ログ化（保留、実資金移行後に対応）**~~ → **完了（2026-07-29、手数料）/完了（2026-07-30、スリッページ）**。手数料は3.4.30で対応済み（ENTRY・SL決済・板監視TP・強制決済のいずれも実際の手数料を反映、取得失敗時は理論値フォールバック）。スリッページ（板監視TPの実約定価格）は当初未対応のまま残っていたが、GMOの取引履歴との突き合わせでダッシュボードとの乖離を確認し、3.4.37で対応した。ENTRY時に即時反映される`jpy_balance`の`balance_delta`計算のみ理論値のまま残っており、これは低優先度として別途記録（下記）
- **pendingエントリー注文の起動時突き合わせが手薄（低優先度、2026-07-29発見、詳細は3.4.29）**: 起動時のポジション復元処理は、建玉が存在するケースの突き合わせは行うが、pendingエントリー注文がGMO側で実際にまだ有効かどうかは能動的に確認していない。実害は緊急停止時のERR-5122/5123ベニン処理や3.4.26のタイムアウト機構で吸収されるため対応は見送り（なお、2026-07-30の実地調査ではこの経路自体は設計通り機能していることを確認済み。詳細は3.4.31・3.4.32）
- ~~エンジンログのバッファリングによる可視性の低下（低優先度、2026-07-29発見、詳細は3.4.29）~~ → **完了（2026-07-30）**。real-test環境について`PYTHONUNBUFFERED=1`の設定、およびエンジンログのファイル永続化（コンテナ削除後も残る）を実装した。詳細は3.4.33
- ~~VPS本番用`docker-compose.yml`のログ永続化・`PYTHONUNBUFFERED`未対応（2026-07-30発見、詳細は3.4.33）~~ → **完了（2026-07-30）**。real-test環境と同様の対応（`run_engine_with_file_logs.py`起動・`PYTHONUNBUFFERED=1`）を本番用compose定義にも反映し、使い捨ての検証用パスでログ永続化・バッファリング解消を実機（ローカルDocker）確認済み。詳細はv66変更点参照
- **`deploy/systemd/`配下ファイルの改行コード・タイムアウト・パス指定の是正（2026-07-30対応）**: 一部ファイルにCRLF混入・`ai-review.service`のタイムアウト未設定・`ExecStart`の書き方不統一があったため修正した。詳細はv66変更点参照
- **VPS移行後の並行運用方針（2026-07-30決定）**: VPS本番稼働後、real-test環境（実資金隔離テスト）は本番と並行運用しない。real-testで見つかっていた不具合は今後本番側でその都度対応する。仮想資金でのシミュレーション検証も、システムをシンプルに保つ目的でVPS・ローカルいずれでも本番と並行して残さない方針とした。本番切替手順（4.5「ネイティブ→Docker本番切り替え」）には、切替前にreal-test環境が停止していることを確認する一文を追記する必要がある（`VPS構築手順書.md`側で対応、9章「両compose同時起動時の二重発注防止」参照）
- **config.jsonの本番引き継ぎ（2026-08-01決定）**: `config/config.json`は環境固有ファイルとして`.gitignore`対象のため、VPSへの`git clone`/`git pull`では転送されない。ネイティブ運用中にAI夜間レビューで調整されてきた時間帯プロファイル設定（TP/SL幅・imbalanceエントリー閾値等、2週間分の学習結果）を本番でも活かすため、VPS移行時にネイティブ側の`config.json`を手動でVPSへコピーする必要がある。コピーは`pending_rollouts`が空の確定状態（03:30のAI夜間レビュー・06:00の設定反映後）で行い、コピー後は`trading_mode`が`"real"`になっているか確認すること。具体的な手順は`VPS構築手順書.md`3.4節に追記済み
- **仮想モードの手数料理論値`TAKER_FEE_RATE=0.0`への手動変更（2026-07-30、詳細は3.4.34）**: Cursorのレビュー・テスト実行を経ない手動編集のため、既存テストへの影響が未確認。将来資金規模が大きくなり実手数料が0円でなくなった場合は値の再設定が必要
- ~~サーキットブレーカー（発注レート超過）の頻発（2026-07-30発見、詳細は3.4.35）~~ → **完了（2026-07-30）**。imbalance反転によるキャンセル後の再エントリークールダウンを同一価格判定から同一方向判定（5秒）に変更し対応した。効果は経過観察中
- **ENTRY時の`jpy_balance`反映（`balance_delta`）が理論値のまま（低優先度、2026-07-29発見、詳細は3.4.30）**: 発注時点でまだ実際の手数料が分からないため理論値で即時反映され、約定確定時に実手数料が判明しても差分修正は行われない。5分間隔の口座残高整合性チェック（3.4.2）で吸収される範囲のため対応は見送り
- **virtual側`check_account_integrity()`の日またぎ二重差し引き未対応（低優先度、2026-08-02発見、詳細は3.4.43）**: 3.4.43でreal側の比較基準を`daily_start_balance + daily_realized_pnl`に変更する際、virtual側も同じ基準に統一すると、日またぎ時にLONGポジションが残存している場合に既存の想定元本差し引きロジックと二重差し引きになり誤検知することが判明した。実資金には影響しないため対応を見送り、virtual側は従来の`initial_jpy + cumulative_pnl`基準のまま残している
- **`account integrity check`が起動時にしか実行されない（低優先度、2026-08-02指摘）**: 現状、口座残高の整合性は「たまたまエンジンを再起動したタイミング」でしか検知されない設計になっている。3.4.43の対応（realの比較基準を日次自動更新に変更）により`initial_jpy`の陳腐化によるアラートは解消したが、今後何らかの取引パターンで`cumulative_pnl`・`daily_realized_pnl`の計算にごくわずかなズレが生じるバグが入った場合、ズレが積み上がって閾値を超えるまで検知できない可能性が残る。定期実行（例えば毎日1回）する仕組みの追加を将来的に検討する
- **`PARAMETER_ABSOLUTE_BOUNDS`の初期値の妥当性検証**: 現状は初期案であり、実運用データをもとに調整が必要（`max_order_size_btc`の0.01〜0.20という範囲も含む）
- **時間帯プロファイルの区切り自体の妥当性検証**: 現在の3区分・境界時刻は仮のもの。データ蓄積後に見直す
- **口座残高の整合性チェック（実装済み、詳細は3.4.2）**: 2026-07-10に`compare_with_internal_state()`/`run_reconciliation_check()`として実装済み。5分ごとにGMO実APIの建玉・残高を取得し、2回連続で内部状態（`live_state.db`）とのズレを検知したら`manual_stop.flag`で自動停止する。ただし現状は**発注・損益計算そのものの基準を実口座値に置き換える段階には至っておらず、内部シミュレーション値を正としたまま実口座と突き合わせて乖離を検知するのみ**である点に注意。実資金投入時には、発注サイズ計算等の基準を実口座残高ベースに切り替える設計変更が別途必要になる。**なお、実口座残高につないでも「同じポジションを繰り返し決済したことにする」ようなロジック自体のバグは防げない**（7.3参照）。金額の参照元とは独立して、頻度ベースのサーキットブレーカー（3.4.1、実装済み）が必要な理由はここにある
- **動的ポジションサイジングの再導入（保留中）**: 口座残高に応じて購入額を増やすこと自体は理にかなっているが、「都度（取引ごとに）計算」は複利の暴走リスクがあるため避ける（7.3参照）。再導入する場合は以下をセットで実装すること:
  1. リサイズは**1日1回（06:00の再起動時）のみ**とし、日中は固定サイズで運用する
  2. その日の実現損益が、開始時残高の一定割合（例: -10%）を超えて悪化したら、その日はそれ以上新規エントリーしない、という**日中サーキットブレーカー**とセットで導入する
  3. これとは別に、**取引頻度そのもの**（1時間あたりの回数）が通常の異常値を超えたら、金額に関わらず自動停止する仕組みも導入する
  4. `max_order_size_btc`（実装済み、5.4参照）による発注サイズの絶対上限は、動的サイジングを再導入した際の重要な安全弁として機能する
- **Cursorへの指示に含めるべき定型文（運用ルール）**: これまでに「週次メンテナンス処理」「TIMEOUT機能の残骸」「動的サイジング」「ログ絵文字」と、依頼していない機能・書き方が原因のトラブルが繰り返し発生している。今後Cursorへの依頼文の末尾には、以下を定型文として含めること:
  - 「明示的に依頼した内容以外は追加しないこと。改善案があれば、実装せず提案だけにとどめること」
  - 「ログ・コンソール出力に絵文字・特殊なUnicode文字を使わないこと」
  - 「新規テストファイルは必ず`tests/`配下に作成すること（リポジトリルート直下には作成しない）」（2026-08-07追加。7.25で`.gitignore`の`/test_*`ルールをルート直下限定に修正済みだが、新規テストファイル自体がルート直下に作られると当然このルールに引っかかり、`git add -f`での強制追加が繰り返し発生する。原因は`.gitignore`側ではなくファイル配置側にあるため、配置場所を都度明示することで対応する）
- **板TP settle修正のVPS実機での動作未確認（2026-08-03設計・実装、2026-08-06反映確認済み、詳細は3.4.47）**: benign cancel時の建玉確認分岐、およびclose後settleの例外分離という2件の対応は、VPS実機（稼働中の`btc-engine`コンテナ）への反映が2026-08-06に確認済み（SNSリール動画対応コミットに同梱される形で既にpull・反映されていた）。残るのは、次回実際に板TPが発動した際のログで`SETTLE FAILED`のCRITICALが出ていないことを確認する動作確認のみ。
- ~~**Instagramリール動画の実資金対応・VPS実機での動作未確認（2026-08-03設計・実装、詳細は4.7.5）**~~ → **完了（2026-08-07）**。冒頭フックのヘッダー常時表示・実資金バッジ・フェードイン・実資金文言について、フレーム単位の見た目確認を実施（フェードイン完了フレームで文字が白くはっきり見えることを確認）。あわせてサムネイル位置の不具合（下記4.7.8参照）も発見・対応した。
- **ERR-5008（GMOタイムスタンプずれエラー）の根本原因が未確定（2026-08-04調査、診断ログは2026-08-07追加・詳細は3.4.50）**: 3.4.44時点では「VPSのクロックドリフトが遠因の可能性」としていたが、2026-08-04にVPS上でNTP同期状態を調査した結果、`ntpsec`により正常に同期済み（オフセット約+0.6〜1.2ms、GMO APIの`Date`ヘッダーと秒単位で一致）であることが確認され、常時のクロックドリフトが原因である可能性は低いと判明した。残る仮説（署名〜送信遅延／GMO側許容窓の狭さ）を次回発生時に切り分けられるよう、2026-08-07に診断ログ（`sign_to_send_ms`・`rtt_ms`等）を追加しVPSへ反映済み。次回ERR-5008発生時のログ確認待ち。
- **孤児注文監視の週次メンテナンス時間帯誤アラート修正（2026-08-08実装・VPS反映完了、詳細は3.4.51／v89）**: 当初の11:00終了判定では境界（11:00ちょうど）で1通誤アラートが発生したため、GMOの「プレ・オープン」仕様（メンテ終了後11:00〜11:10は取消のみ受付）に合わせて実効終了を11:10へ再修正し、VPS反映・動作確認まで完了。次回8/15（土）のメンテナンス時間帯で問題が再発しないかは引き続き経過観察
- **AI夜間レビューのcanary方式が実行回数ベースで進む点への運用上の注意（2026-08-04判明、詳細は3.4.46）**: 5.4.2の段階適用（[30%, 60%, 100%]）は暦日ではなく`review_pipeline`が実行された回数で`day_index`を進める設計になっている。そのため、`ai-review.timer`による通常の03:30定時実行以外に日中手動で`ai_review`を実行すると、進行中のロールアウトがあれば意図せず段階を1つ進めてしまう。手動実行の前には必ず`config.json`の`pending_rollouts`が空であることを確認するか、進行中であれば手動実行を見送り定時実行に任せること
- **週次・月次SNSレポート（構想段階、2026-08-07方針決定・未着手）**: 現行の日次レポート（累積損益等を含む）に加え、期間で区切った週次・月次のまとめ投稿を新設する方針。日次は「淡々とした記録・信頼の継続性」、週次・月次は「区切られた期間で完結する物語性のあるまとめ・新規層への訴求」という役割分担とする。
  - 対象期間・投稿タイミング: 週次は月〜日を対象期間とし月曜に投稿。月次は暦月（1日〜末日）を対象期間とし翌月1日に投稿、過去数ヶ月分の月単位推移も見せる。月曜と月初1日が重なる場合は、同時に負荷が集中しないよう週次・月次で投稿時間帯をずらす
  - X: 週次・月次ともテキストのみ（コスト抑制のため画像添付は行わない）。既存の日次許可リスト方式（逆算不可能な指標のみ公開）の考え方を踏襲する
  - Instagram: 棒グラフが1本ずつ伸びる／出現するアニメーション動画として見せる方向で決定。日次リール（4.7.5）で構築済みのBGM合成（`mux_bgm_into_video()`）・公開配信インフラ（専用サブドメイン・トークン付きURL・自動削除）をそのまま流用する想定。日次カード型テンプレートの軽量流用案（見出しのみ差し替え）は今回は採らない
  - データ集計は`daily_history.jsonl`を週・月単位に再集計する形を想定。直近平均との比較で使っている`compute_recent_averages()`（4.7.5系）と同様の考え方を流用できる見込み
  - 着手はSNS運用データが十分に貯まってから（実資金運用開始は2026-08-01のため、本方針決定時点ではまだ数日分のデータしかない）。現時点ではCursorへの実装指示・詳細設計（グラフ生成方式、Instagramの新規投稿種別追加等）は未着手
- **長時間停止（PAUSED継続）検知の監視が未実装（2026-08-21判明、詳細は7.27）**: 2026-08-20の15時間停止インシデントで、既存の心拍監視（`check_monitor_heartbeats.py`等）は`live_state.db`の`updated_at`更新自体は検知できていたが、`manual_stop`にPAUSED状態が異常に長引いていること自体を検知してアラートする仕組みはない。優先度を見て別途対応を検討する
- **`check_csv_db_consistency.py`の`EXIT_REASONS`に`FORCE_CLOSE_REAL`が含まれていない（2026-08-21判明、詳細は7.27）**: `FORCE_CLOSE_REAL`発生時に毎回恒常的にdiff=+1の見かけ上のmismatchアラートが出る構造的な抜け。実害はない（累積DB・CSVのカウンタ自体は正しく、チェック対象の抜けのみ）ので優先度低め、`EXIT_REASONS`への追加を今後の対応候補として記録

---

## 10. 用語集

| 用語 | 意味 |
|---|---|
| Maker / Taker | 指値注文で板に並ぶ（Maker）か、即時約定する成行的注文（Taker）か |
| Imbalance | 買い圧力 = best_bid_size / (best_bid_size + best_ask_size) |
| プロファイル | 時間帯ごとに異なるStrategyConfigパラメータのセット |
| config_version | 設定ファイルのバージョン識別子（YYYY-MM-DD_HH-MM形式） |
| confidence | AI議論用サマリーにおける、サンプル数・データ充足度に基づく信頼度ラベル |
| サーキットブレーカー | 根拠の吟味を経由せず、条件（発注レート・口座整合性等）を満たしたら機械的に緊急停止する安全装置（3.4） |
| 外れ値判定 / 段階適用（カナリア方式） | AI提案の変更幅が過去の傾向から外れていないかを統計的に判定し、外れていれば3日かけて段階的に反映する仕組み（5.4.2） |
| pending_rollouts | 段階適用中のパラメータの進捗（開始値・目標値・現在値・日数）を保持する`config.json`内のセクション |
| デプロイフロー | ローカルでの変更をGitHub経由でVPS本番環境へ反映する手順（`git push`→`git pull`、土曜メンテナンス枠で実施予定、詳細は4.4） |
| Dockerイメージ / コンテナ | Pythonの実行環境ごとアプリを封じ込めた「設計図（イメージ）」と「実行中の状態（コンテナ）」。ローカル（Windows）とVPS（Ubuntu）で同一イメージを動かすことでOS差異による不具合を防ぐ（詳細は4.5） |
| bind mount | ホストPC上のフォルダ・ファイルをコンテナ内から見えるようにする仕組み。`config.json`や`log/`等、環境固有の状態はイメージに焼き込まずbind mountで渡す方針としている |
| `runtime/`フォルダ | `manual_stop.flag`・`live_state.db`・`trading_engine.pid`・`anomaly_state.json`等、実行時に書き換わる状態ファイルを集約する専用フォルダ（案B、4.5.1） |
| systemdタイマー | Ubuntu標準のスケジュール実行機構。cronと異なり`systemctl list-timers`で次回実行予定、`journalctl`で過去の実行結果・exit codeを確認できる。AI夜間レビューのVPS実行方式として採用（4.5.4） |
| Docker Compose `profiles` | 特定のプロファイル指定時のみ解決されるサービスを定義する仕組み。`ai_review`サービスに`profiles: [manual]`を指定し、`docker compose up`で誤って起動しないようにしている（4.5.3） |
