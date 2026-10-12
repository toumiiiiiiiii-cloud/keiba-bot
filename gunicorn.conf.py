# Renderで動かすサーバー（gunicorn）の設定。
# LINEが画像を取りに来たときに、返信の処理中でも同時に対応できるようにする。
workers = 1      # 画像を /tmp に保存しているので、プロセスは1つにする
threads = 8      # 同時に8件まで処理できる
timeout = 120    # 初回のフォントダウンロードに時間がかかっても落ちないように
graceful_timeout = 30   # 更新で再起動するとき、処理中の予想を送り終えるまで待つ


def worker_int(worker):
    _wait()


def worker_exit(server, worker):
    _wait()


def _wait():
    try:
        import main
        main.wait_active(25)
    except Exception as e:
        print(f"[shutdown] 待てませんでした: {e}", flush=True)
