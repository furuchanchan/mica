from http.server import ThreadingHTTPServer
import mica_config, shodan_live
shodan_live.PORT = mica_config.app_port()
ThreadingHTTPServer(('127.0.0.1', shodan_live.PORT), shodan_live.Handler).serve_forever()
