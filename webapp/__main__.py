import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app/src/main/python'))
from .app import create_app
from waitress import create_server


def main():
    app = create_app(os.environ.get('KTX_DATA_DIR', '/data'))
    controller = app.extensions['ktx']
    server = create_server(app, host='0.0.0.0', port=8080, threads=4, max_request_body_size=32768)
    def stop(signum, frame): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        print('KTX web server ready (container port 8080)', flush=True)
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        controller.close()
        server.close()


if __name__ == '__main__': main()
