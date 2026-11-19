"""設定した送り先へだけ通信する。転送（リダイレクト）には従わない。この Mac の中の宛先へは、プロキシを通さない。"""
import urllib.error
import urllib.parse
import urllib.request

import mica_config


def validate_url(url, base=False):
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.fragment and (not base or not parsed.query)
                 and not any(ord(char) <= 32 or ord(char) == 127 for char in url)
                 and '\\' not in url and (parsed.port is None or 1 <= parsed.port <= 65535))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('送信先の URL が不正です。http/https の URL を、認証情報を含めずに設定してください。')
    return parsed


def is_local(url):
    return validate_url(url).hostname in ('localhost', '127.0.0.1', '::1')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        raise urllib.error.HTTPError(request.full_url, code, '送信先の転送には従いません', headers, response)


def urlopen(request, timeout):
    if isinstance(request, str):
        request = urllib.request.Request(request)
    parsed = validate_url(request.full_url)
    if not mica_config.cloud_allowed() and not is_local(request.full_url):
        raise RuntimeError('MICA_NO_CLOUD=1 のため、Mac の外へは送りません。設定した送信先を確かめてください。')
    # localhost の名前解決を外部のアドレスへ向けさせない。
    if parsed.hostname == 'localhost':
        address = '127.0.0.1' + (':' + str(parsed.port) if parsed.port else '')
        request.full_url = urllib.parse.urlunsplit(parsed._replace(netloc=address))
    # Mac の外の宛先は、使う人のネットワークの設定（プロキシ）に従う。Mac の中の宛先は、プロキシへ出さない
    handlers = [NoRedirect()] + ([urllib.request.ProxyHandler({})] if is_local(request.full_url) else [])
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)
