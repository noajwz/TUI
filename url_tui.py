#!/usr/bin/env python3
"""URL inspector — takes a link apart and says what each piece does: the scheme, who the host
really is (subdomains, the registered domain, its top-level domain, IP addresses in disguise,
look-alike letters), the port, the path, every query parameter (tracking tags, secrets, a second
URL to be sent on to) and the fragment. Warnings come first, a cleaned-up link without the
tracking is one key away, and a parameter holding another URL can be inspected in turn.

It works offline: nothing is fetched until you press `r`, which follows the link's redirects with
HEAD requests and shows where it ends up (it doesn't run the page, so JavaScript redirects aren't
seen). Without an argument it takes a URL from the clipboard.
"""

import argparse
import base64
import binascii
import curses
import ipaddress
import json
import locale
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

MAX_HOPS = 10
TIMEOUT = 8
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) url_tui"
TICK_MS = 150
MESSAGE_SECONDS = 4
MIN_W, MIN_H = 50, 14
URL_LINES = 4              # the coloured URL at the top takes at most this many lines

SCHEMES = {
    "https": "HTTP over TLS: the connection is encrypted, and the server proved with a certificate that it "
             "owns this name. It says nothing about whether the site itself is honest — phishing sites have "
             "certificates too.",
    "http": "Plain HTTP: nothing is encrypted, so anyone on the network between you and the server (public "
            "Wi-Fi, the provider) can read and change what is sent. Most sites redirect it to https.",
    "ftp": "File Transfer Protocol, unencrypted. Browsers no longer open these; an FTP client does.",
    "sftp": "File transfer over SSH, encrypted. Opens in an SFTP client.",
    "ws": "WebSocket, unencrypted: a two-way channel a web page keeps open to a server.",
    "wss": "WebSocket over TLS: an encrypted two-way channel a web page keeps open to a server.",
    "mailto": "Opens your mail app with a new message to the address. Nothing is sent until you press send.",
    "tel": "Offers to call the phone number.",
    "sms": "Opens a new text message to the number.",
    "file": "A file on this computer or a network share — not a web address. Opening it runs whatever app "
            "handles that type of file.",
    "data": "The content is inside the URL itself, so no server is contacted. Normal for small images; a data: "
            "URL holding HTML is a phishing trick, because there's no domain in the address bar to check.",
    "javascript": "Runs JavaScript in the page you're on when clicked. Never paste one into the address bar "
                  "because someone told you to — it can act as you on that site.",
    "magnet": "A torrent link. It names files by their hash; your torrent client asks other peers for them.",
    "ssh": "A Secure Shell login to the host.",
    "git": "The Git protocol, unauthenticated and unencrypted, read-only.",
    "smb": "A Windows (SMB/Samba) network share. Opening it makes your computer connect to that server and "
           "may send your login to it.",
    "vnc": "A remote-desktop (VNC) connection.",
    "blob": "An object held in the browser's memory by a page; it only means something in the tab that made it.",
    "about": "A page built into the browser.",
    "chrome": "A page built into Chrome.",
    "view-source": "Shows the HTML source of the page after it.",
}
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21, "ssh": 22, "sftp": 22, "git": 9418,
                 "smb": 445, "vnc": 5900, "ldap": 389, "ldaps": 636, "rtsp": 554, "irc": 6667, "ircs": 6697}
PORT_NOTES = {8080: "a common port for proxies and development servers", 8000: "a common development-server port",
              8888: "a common development port (Jupyter uses it)", 3000: "a common development-server port (Node)",
              5000: "a common development-server port (Flask)", 8443: "a common alternative HTTPS port",
              22: "the SSH port", 21: "the FTP port", 25: "the mail-server (SMTP) port", 3389: "Windows Remote Desktop"}

# a few two-part public suffixes; without the full Public Suffix List this is a guess
TWO_PART_SUFFIXES = set("""co.uk org.uk ac.uk gov.uk me.uk ltd.uk plc.uk net.uk sch.uk nhs.uk com.au net.au org.au
edu.au gov.au co.nz org.nz govt.nz co.jp ne.jp or.jp ac.jp go.jp co.kr or.kr com.br net.br org.br gov.br com.cn
net.cn org.cn gov.cn com.tw com.hk co.in net.in org.in gov.in ac.in co.za org.za gov.za com.mx com.ar com.tr
co.il com.sg com.my co.id com.ph com.vn com.ua com.pl com.es co.th com.eg com.sa com.pk com.ng github.io
gitlab.io herokuapp.com vercel.app netlify.app pages.dev workers.dev blogspot.com azurewebsites.net
cloudfront.net amazonaws.com appspot.com web.app firebaseapp.com""".split())
TLDS = {"com": "commercial — open to anyone", "org": "organisations — open to anyone", "net": "networks — open to anyone",
        "edu": "US colleges and universities", "gov": "the US government", "mil": "the US military",
        "int": "international treaty organisations", "io": "the British Indian Ocean Territory, popular with tech companies",
        "ai": "Anguilla, popular with AI companies", "co": "Colombia, sold as a .com alternative",
        "me": "Montenegro, sold for personal sites", "tv": "Tuvalu, sold for video sites", "ly": "Libya, used by shorteners",
        "dev": "Google's TLD for developers; browsers only connect to it over HTTPS",
        "app": "Google's TLD for apps; browsers only connect to it over HTTPS",
        "zip": "a Google TLD that looks like a file name — a known trick for fake download links",
        "mov": "a Google TLD that looks like a file name — a known trick for fake download links",
        "xyz": "generic and cheap — widely used, and by spammers too", "top": "generic and cheap, popular with spammers",
        "info": "generic", "biz": "business", "shop": "shops", "online": "generic", "site": "generic",
        "onion": "a Tor hidden service, only reachable through the Tor browser", "local": "this local network (mDNS)",
        "localhost": "this computer", "test": "reserved for testing; never on the internet", "example": "reserved for examples",
        "internal": "reserved for private networks", "arpa": "internet infrastructure (reverse DNS)"}
COUNTRIES = {"nl": "the Netherlands", "be": "Belgium", "de": "Germany", "fr": "France", "uk": "the United Kingdom",
             "us": "the United States", "eu": "the European Union", "es": "Spain", "it": "Italy", "pl": "Poland",
             "se": "Sweden", "no": "Norway", "dk": "Denmark", "fi": "Finland", "ch": "Switzerland", "at": "Austria",
             "ie": "Ireland", "pt": "Portugal", "cz": "Czechia", "ru": "Russia", "ua": "Ukraine", "cn": "China",
             "jp": "Japan", "kr": "South Korea", "in": "India", "au": "Australia", "nz": "New Zealand", "ca": "Canada",
             "br": "Brazil", "mx": "Mexico", "za": "South Africa", "tk": "Tokelau (free domains, much abused)",
             "su": "the former Soviet Union", "cc": "the Cocos Islands", "gg": "Guernsey, popular with gaming sites",
             "sh": "Saint Helena", "to": "Tonga", "ws": "Samoa", "fm": "Micronesia", "is": "Iceland", "lu": "Luxembourg"}
SHORTENERS = set("""bit.ly bitly.com t.co tinyurl.com goo.gl ow.ly is.gd buff.ly rebrand.ly lnkd.in t.ly cutt.ly
shorturl.at rb.gy tiny.cc s.id v.gd qr.ae trib.al dlvr.it shorte.st adf.ly bl.ink short.io amzn.to amzn.eu youtu.be
fb.me spoti.fi apple.co aka.ms msft.it wp.me git.io db.tt forms.gle maps.app.goo.gl g.co t.me""".split())
SITES = {"google": "Google", "youtube": "YouTube", "youtu.be": "YouTube", "facebook": "Facebook", "instagram": "Instagram",
         "twitter": "Twitter/X", "x.com": "X (Twitter)", "github": "GitHub", "gitlab": "GitLab", "wikipedia": "Wikipedia",
         "amazon": "Amazon", "apple": "Apple", "microsoft": "Microsoft", "live": "Microsoft", "office": "Microsoft",
         "outlook": "Microsoft Outlook", "linkedin": "LinkedIn", "reddit": "Reddit", "spotify": "Spotify",
         "netflix": "Netflix", "paypal": "PayPal", "bol": "bol.com", "ing": "ING", "rabobank": "Rabobank",
         "abnamro": "ABN AMRO", "digid": "DigiD", "belastingdienst": "Belastingdienst", "postnl": "PostNL",
         "marktplaats": "Marktplaats", "coinbase": "Coinbase", "anthropic": "Anthropic", "claude": "Claude",
         "openai": "OpenAI", "dropbox": "Dropbox", "whatsapp": "WhatsApp", "tiktok": "TikTok", "ebay": "eBay"}

# query parameters: name -> (tag, explanation); tag is track, secret, redirect or info
TRACK = "track"
PARAMS = {
    "utm_source": (TRACK, "Google Analytics campaign tag: which site, newsletter or app the click came from."),
    "utm_medium": (TRACK, "Google Analytics campaign tag: the kind of channel — email, social, cpc (paid ads) …"),
    "utm_campaign": (TRACK, "Google Analytics campaign tag: the name of the marketing campaign."),
    "utm_term": (TRACK, "Google Analytics campaign tag: the paid search keyword."),
    "utm_content": (TRACK, "Google Analytics campaign tag: which link or ad in the campaign was clicked."),
    "utm_id": (TRACK, "Google Analytics campaign ID."),
    "fbclid": (TRACK, "Facebook click ID: ties this visit to your Facebook account's click."),
    "gclid": (TRACK, "Google Ads click ID: ties this visit to the ad you clicked."),
    "gbraid": (TRACK, "Google Ads click ID for iOS app campaigns."),
    "wbraid": (TRACK, "Google Ads click ID for web-to-app campaigns."),
    "dclid": (TRACK, "Google Display & Video click ID."),
    "gad_source": (TRACK, "Google Ads: where the ad was shown."),
    "msclkid": (TRACK, "Microsoft Ads (Bing) click ID."),
    "twclid": (TRACK, "X/Twitter ads click ID."),
    "ttclid": (TRACK, "TikTok ads click ID."),
    "li_fat_id": (TRACK, "LinkedIn ads member ID."),
    "yclid": (TRACK, "Yandex ads click ID."),
    "igshid": (TRACK, "Instagram share ID: who shared the link with you."),
    "igsh": (TRACK, "Instagram share ID: who shared the link with you."),
    "si": (TRACK, "Share ID (YouTube, Spotify): links the link to the account that shared it."),
    "mc_cid": (TRACK, "Mailchimp campaign ID."),
    "mc_eid": (TRACK, "Mailchimp subscriber ID: which recipient clicked."),
    "_hsenc": (TRACK, "HubSpot email tracking."),
    "_hsmi": (TRACK, "HubSpot email tracking."),
    "mkt_tok": (TRACK, "Marketo email tracking: which recipient clicked."),
    "_ga": (TRACK, "Google Analytics client ID carried across sites."),
    "_gl": (TRACK, "Google Analytics linker: carries your visitor ID across sites."),
    "oly_enc_id": (TRACK, "Omeda subscriber tracking."),
    "vero_id": (TRACK, "Vero email tracking."),
    "s_cid": (TRACK, "Adobe Analytics campaign ID."),
    "ref_src": (TRACK, "Referral tag: where the link was shared from."),
    "ref_url": (TRACK, "Referral tag: the page the link was shared from."),
    "spm": (TRACK, "Alibaba/AliExpress tracking."),
    "trk": (TRACK, "LinkedIn tracking."),
    "trackingid": (TRACK, "Tracking ID."),
    "srsltid": (TRACK, "Google Merchant click ID."),
    "epik": (TRACK, "Pinterest ads click ID."),
    "sc_cid": (TRACK, "Snapchat ads click ID."),
    "rdt_cid": (TRACK, "Reddit ads click ID."),
    "pk_campaign": (TRACK, "Matomo campaign tag."),
    "mtm_campaign": (TRACK, "Matomo campaign tag."),
    "q": ("info", "Search terms, or on a redirect page the address to go to."),
    "query": ("info", "Search terms."), "search": ("info", "Search terms."), "s": ("info", "Often search terms."),
    "page": ("info", "Which page of results."), "p": ("info", "Often a page number or a post ID."),
    "lang": ("info", "Language."), "hl": ("info", "Interface language (Google)."), "gl": ("info", "Country (Google)."),
    "v": ("info", "On YouTube, the video ID."), "list": ("info", "On YouTube, the playlist ID."),
    "t": ("info", "Often a start time (YouTube: seconds, or 1m30s)."), "id": ("info", "An ID of something on the site."),
    "ref": ("info", "A referral or affiliate tag — who gets credit for sending you."),
    "tag": ("info", "On Amazon, the affiliate who earns a commission from the purchase."),
    "aff_id": ("info", "Affiliate ID: who earns a commission from the purchase."),
    "affiliate": ("info", "Affiliate ID: who earns a commission from the purchase."),
    "sort": ("info", "Sort order."), "filter": ("info", "A filter on the list shown."),
    "format": ("info", "Output format."), "lat": ("info", "Latitude."), "lng": ("info", "Longitude."),
    "client_id": ("info", "OAuth: the app asking you to sign in. Check it's the app you expect."),
    "response_type": ("info", "OAuth: what the app wants back (code, token)."),
    "scope": ("info", "OAuth: what the app is asking permission for. Read it before agreeing."),
    "prompt": ("info", "OAuth: whether to ask you again (login, consent)."),
}
SECRET = {"token", "access_token", "id_token", "refresh_token", "api_key", "apikey", "key", "secret", "client_secret",
          "password", "passwd", "pwd", "pass", "session", "sessionid", "sid", "auth", "authorization", "code",
          "otp", "signature", "sig", "x-amz-signature", "x-amz-credential", "x-goog-signature", "jwt", "ticket",
          "magic", "login_token", "reset_token", "invite"}
REDIRECT = {"url", "u", "redirect", "redirect_url", "redirect_uri", "redirecturl", "redirect_to", "redir", "next",
            "return", "returnto", "return_to", "returnurl", "return_url", "continue", "dest", "destination", "goto",
            "target", "to", "link", "out", "r", "rurl", "forward", "callback", "successurl", "back", "q", "ru"}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
HEX_ID = re.compile(r"[0-9a-f]{16,}", re.I)
B64 = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
RISKY_EXT = {"exe": "a Windows program", "msi": "a Windows installer", "bat": "a Windows batch script",
             "cmd": "a Windows script", "ps1": "a PowerShell script", "scr": "a Windows screensaver (a program)",
             "vbs": "a Windows script", "js": "a script (on Windows it runs outside the browser when opened)",
             "jar": "a Java program", "apk": "an Android app", "dmg": "a Mac disk image", "pkg": "a Mac installer",
             "app": "a Mac app", "sh": "a shell script", "iso": "a disk image", "lnk": "a Windows shortcut",
             "hta": "a Windows HTML application", "docm": "a Word document with macros",
             "xlsm": "an Excel workbook with macros", "zip": "a zip archive", "rar": "a RAR archive", "7z": "a 7-Zip archive"}
FILE_EXT = {"pdf": "a PDF document", "html": "a web page", "htm": "a web page", "php": "a page made by PHP on the server",
            "asp": "a page made by ASP on the server", "aspx": "a page made by ASP.NET on the server",
            "jsp": "a page made by Java on the server", "png": "an image", "jpg": "an image", "jpeg": "an image",
            "gif": "an image", "webp": "an image", "svg": "an image", "mp4": "a video", "mp3": "audio",
            "csv": "a spreadsheet as text", "xlsx": "an Excel workbook", "docx": "a Word document",
            "txt": "plain text", "json": "data (JSON)", "xml": "data (XML)", "ics": "a calendar"}


class Part:
    """One row of the breakdown."""
    def __init__(self, section, label, value, note="", tag="", url=None):
        self.section, self.label, self.value, self.note, self.tag = section, label, value, note, tag
        self.url = url             # another URL inside this one, to inspect with Enter


class Report:
    def __init__(self, raw):
        self.raw = raw
        self.url = raw
        self.parts = []
        self.warnings = []
        self.summary = ""
        self.segments = []         # (text, colour name) of the URL for the coloured line
        self.clean = None          # the URL without tracking parameters, when that differs
        self.hops = None           # redirect chain once followed: [(status, url)]

    def add(self, *args, **kw):
        p = Part(*args, **kw)
        self.parts.append(p)
        return p

    def warn(self, label, note):
        self.warnings.append(Part("Warnings", label, "", note, "warn"))


# -- taking the URL apart --

def unquote(text):
    return urllib.parse.unquote(text, errors="replace")


def script_of(ch):
    try:
        return unicodedata.name(ch).split()[0]
    except ValueError:
        return "?"


def registered_domain(host):
    """(subdomains, registered domain, public suffix) — a guess without the Public Suffix List."""
    labels = host.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_PART_SUFFIXES:
        k = 3
    else:
        k = 2
    if len(labels) < k:
        return "", host, labels[-1] if len(labels) > 1 else ""
    return ".".join(labels[:-k]), ".".join(labels[-k:]), ".".join(labels[-(k - 1):])


def site_name(domain):
    first = domain.split(".")[0]
    return SITES.get(domain) or SITES.get(first)


def decode_jwt(token):
    try:
        head, body = token.split(".")[:2]
        dec = [json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))) for p in (head, body)]
        return dec
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def decode_b64(text):
    try:
        raw = base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4))
        s = raw.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    return s if s and all(c.isprintable() or c in "\n\t" for c in s) else None


def looks_like_url(text):
    t = text.strip()
    return bool(re.match(r"(?:[a-z][a-z0-9+.-]*:|www\.)\S", t, re.I)) and " " not in t and len(t) > 4


def inspect(raw):
    raw = raw.strip()
    r = Report(raw)
    url = raw
    assumed = not re.match(r"[a-z][a-z0-9+.-]*:", url, re.I)
    if assumed:
        url = "https://" + url.lstrip("/")
        r.url = url
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https", "ws", "wss", "ftp", "sftp", "ssh", "git", "smb", "vnc", "file"):
        return inspect_opaque(r, url, scheme)
    sp = urllib.parse.urlsplit(url)
    r.segments.append((sp.scheme + "://", "scheme"))
    r.add("Scheme", "assumed" if assumed else "scheme", sp.scheme,
          ("There was no scheme, so this reads it as https://, as a browser would. " if assumed else "")
          + SCHEMES.get(scheme, "The protocol."))
    if scheme == "http":
        r.warn("Not encrypted", "This is http://, not https://: the page and anything you type into it travel "
               "unencrypted. Don't enter passwords or payment details.")
    netloc = sp.netloc
    userinfo, _, hostport = netloc.rpartition("@")
    if "@" in netloc:
        r.segments.append((userinfo + "@", "user"))
        user, _, password = userinfo.partition(":")
        r.add("Login", "user", unquote(user), "A user name inside the URL. Browsers mostly ignore it, or ask "
              "before using it.")
        if password:
            r.add("Login", "password", unquote(password), "A password written into the link: anyone who sees "
                  "the link has it.", "secret")
            r.warn("Password in the link", "The link carries a password in plain text.")
        if "." in user or scheme.startswith("http"):
            r.warn("Text before @", f"Everything before the @ is a user name, not the site: this goes to "
                   f"{sp.hostname or '?'}, not to {unquote(user)}. A classic way to make a link look like "
                   "it goes somewhere else.")
    host = sp.hostname or ""
    try:
        port = sp.port
        bad_port = False
    except ValueError:
        port, bad_port = None, True
    host_text = hostport.rsplit(":", 1)[0] if ":" in hostport and not hostport.endswith("]") else hostport
    r.segments.append((host_text, "host"))
    if hostport != host_text:
        r.segments.append((hostport[len(host_text):], "port"))
    inspect_host(r, host, scheme)
    if bad_port:
        r.add("Port", "port", hostport[len(host_text) + 1:], "Not a valid port number; the browser won't open this.",
              "warn")
    elif port is not None:
        default = DEFAULT_PORTS.get(scheme)
        if port == default:
            note = f"{port} is already the default for {scheme}, so writing it changes nothing."
        else:
            note = (f"A non-standard port: {scheme} normally uses {default}." if default else "The port.") + \
                   (f" {port} is {PORT_NOTES[port]}." if port in PORT_NOTES else "")
        r.add("Port", "port", str(port), note)
    elif DEFAULT_PORTS.get(scheme):
        r.add("Port", "port", f"{DEFAULT_PORTS[scheme]} (default)", f"No port given, so {scheme}'s default.", "dim")
    inspect_path(r, sp.path, host)
    if sp.query:
        r.segments.append(("?", "punct"))
        inspect_query(r, sp.query, host)
    if sp.fragment:
        r.segments.append(("#" + sp.fragment, "fragment"))
        inspect_fragment(r, sp.fragment)
    elif url.endswith("#"):
        r.segments.append(("#", "fragment"))
    inspect_site(r, sp, host)
    r.summary = summarise(r, sp, host)
    clean = clean_url(sp)
    if clean != url:
        r.clean = clean
    return r


def inspect_host(r, host, scheme):
    if not host:
        if scheme != "file":
            r.add("Host", "host", "(none)", "There's no host, so there's nowhere to go.", "warn")
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is None and re.fullmatch(r"[0-9a-fx.]+", host, re.I) and not re.search(r"[g-wyz]", host, re.I):
        try:
            dotted = socket.inet_ntoa(socket.inet_aton(host))
            ip = ipaddress.ip_address(dotted)
            r.warn("Disguised IP address", f"“{host}” is the IP address {dotted} written as a single number, or in "
                   "hex or octal. Real sites don't do this; it hides where the link goes.")
        except (OSError, ValueError):
            pass
    if ip:
        kind = ("this computer (loopback)" if ip.is_loopback else
                "a cloud server's metadata service — reaching it from a link is an attack" if str(ip) == "169.254.169.254" else
                "a link-local address on your own network segment" if ip.is_link_local else
                "a private address, only reachable inside a local network" if ip.is_private else
                "a multicast address" if ip.is_multicast else
                "an address that isn't routed on the internet" if ip.is_reserved or ip.is_unspecified else
                "a public internet address")
        r.add("Host", f"IPv{ip.version} address", str(ip), f"The link names a computer by number, not by a domain: "
              f"{kind}. There's no name to check, which is why phishing links sometimes use one.", "warn" if ip.is_global else "")
        if ip.is_global:
            r.warn("Bare IP address", "The link goes to an IP address instead of a named site.")
        return
    shown = host
    if "xn--" in host:
        try:
            shown = host.encode("ascii").decode("idna")
        except UnicodeError:
            shown = host
    elif not host.isascii():
        try:
            puny = host.encode("idna").decode("ascii")
            r.add("Host", "punycode", puny, "How the name is really sent over DNS (punycode).", "info")
        except UnicodeError:
            pass
    if shown != host:
        r.add("Host", "displayed as", shown, f"“{host}” is punycode for an internationalised name with non-ASCII "
              "letters. Some browsers show the letters, some the xn-- form.", "info")
    letters = [c for c in shown if c.isalpha() and not c.isascii()]
    if letters:
        for label in shown.split("."):
            scripts = {script_of(c) for c in label if c.isalpha()}
            if len(scripts) > 1:
                names = sorted(s.title() for s in scripts)
                r.warn("Mixed alphabets", f"“{label}” mixes {', '.join(names[:-1])} and {names[-1]} letters. "
                       "That's how look-alike domains are made (a Cyrillic а in place of a Latin a).")
                break
        else:
            r.warn("Non-Latin letters", "The name has letters outside the Latin alphabet. Check that it isn't a "
                   "look-alike of a site you know.")
    sub, domain, suffix = registered_domain(shown.rstrip("."))
    name = site_name(domain)
    r.add("Host", "host", host, "The computer the browser connects to. Everything else in the link is up to "
          "that site.")
    r.add("Host", "domain", domain, f"The registered domain — whoever owns it controls everything under it"
          + (f". This is {name}'s." if name else ".") + " This is the part to check.", "key")
    if sub:
        note = "Subdomains are set up by the domain's owner, so they say nothing about who you're talking to."
        if sub == "www":
            note = "www is a subdomain kept by convention; it's usually the same site as without it."
        r.add("Host", "subdomain", sub, note)
        labels = sub.split(".")
        tlds = {"com", "net", "org", "nl", "de", "be", "uk", "co", "io", "gov", "edu", "eu", "fr"}
        brand = [lab for lab in labels if site_name(lab) and site_name(lab) != name]
        if any(lab in tlds for lab in labels) or brand:
            r.warn("Look-alike name", f"The site is {domain}. “{sub}” in front of it is only a subdomain "
                   f"{domain}'s owner chose — it doesn't make this {sub.split('.')[0]}'s site.")
    if suffix:
        last = suffix.split(".")[-1]
        what = TLDS.get(last) or (f"the country code of {COUNTRIES[last]}" if last in COUNTRIES else
                                  "a country code" if len(last) == 2 else "a generic top-level domain")
        r.add("Host", "top-level domain", "." + suffix, f".{last} is {what}.")
        if last in ("zip", "mov"):
            r.warn("File-name domain", f".{last} is a real top-level domain: “{domain}” is a website, not a file.")
    if domain in SHORTENERS or host in SHORTENERS:
        r.warn("Link shortener", f"{domain} is a link shortener: the real destination is hidden until you follow "
               "it. Press r to see where it goes without opening it.")
    if shown.count("-") >= 3 or len(shown) > 60:
        r.warn("Unusual host name", "A long name or many hyphens is common in throwaway phishing domains.")


def inspect_path(r, path, host):
    if not path:
        return
    r.segments.append((path, "path"))
    segs = path.split("/")[1:] if path.startswith("/") else path.split("/")
    if path == "/":
        r.add("Path", "path", "/", "The root: the site's home page.")
        return
    r.add("Path", "path", unquote(path), "Which page or file on the site. What it means is entirely up to the "
          "site; it can't send you anywhere else.")
    for k, seg in enumerate(segs, 1):
        if not seg:
            continue
        text = unquote(seg)
        note, tag = "", ""
        if UUID.fullmatch(text):
            note = "Looks like an ID (a UUID) — often unique to you, an order or a document."
        elif HEX_ID.fullmatch(text) or (len(text) >= 20 and re.fullmatch(r"[A-Za-z0-9_-]+", text) and re.search(r"\d", text)
                                         and re.search(r"[A-Za-z]", text)):
            note = "Looks like an ID or a token. If it's unique to you, the link is too — think before sharing."
            if JWT.fullmatch(text):
                note, tag = jwt_note(text), "secret"
        elif text in ("..", "."):
            note, tag = "A dot segment: moves up a folder. Browsers resolve it before sending.", "warn"
        elif "%2f" in seg.lower() or "%25" in seg:
            note, tag = "Encoded slashes or a double-encoded %: a way to slip past filters.", "warn"
        if k == len(segs) and "." in text:
            ext = text.rsplit(".", 1)[1].lower()
            if ext in RISKY_EXT:
                note, tag = f"The file is {RISKY_EXT[ext]}. Only open it if you were expecting it.", "warn"
                r.warn("Download", f"The link points straight at {RISKY_EXT[ext]} (“{text}”).")
            elif ext in FILE_EXT:
                note = f"The file name says it is {FILE_EXT[ext]} — the server decides what it really sends."
        if note:
            r.add("Path", f"segment {k}", text, note, tag)
    if path.endswith("/") and len(path) > 1:
        r.add("Path", "trailing /", "", "A trailing slash usually means a folder-like page; most sites treat "
              "both forms the same.", "dim")
    if host == "urldefense.com" and "/v3/__" in path:
        inner = path.split("/v3/__", 1)[1].split("__;", 1)[0]
        r.add("Path", "wrapped link", inner, "Proofpoint URL Defense: your mail system rewrote the link to check "
              "it when clicked. This is the original. ⏎ inspects it.", "url", url=inner)


def jwt_note(token):
    dec = decode_jwt(token)
    if not dec:
        return "Looks like a JWT (a signed login token), but doesn't decode."
    head, body = dec
    claims = ", ".join(f"{k}={v}" for k, v in list(body.items())[:6])
    exp = body.get("exp")
    when = ""
    if isinstance(exp, (int, float)):
        when = " It expires " + time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)) + \
               (" (already expired)." if exp < time.time() else ".")
    return f"A JWT — a signed token that usually logs you in. Anyone with it can act as you until it expires.{when} " \
           f"Signed with {head.get('alg', '?')}; claims: {claims}"


def inspect_query(r, query, host):
    pairs = query.split("&")
    for n, pair in enumerate(pairs):
        if n:
            r.segments.append(("&", "punct"))
        key, eq, value = pair.partition("=")
        r.segments.append((key, "key"))
        if eq:
            r.segments.append(("=" + value, "value"))
        name = unquote(key.replace("+", " "))
        val = unquote(value.replace("+", " "))
        low = name.lower()
        tag, note = PARAMS.get(low, ("", ""))
        if not tag and low.startswith(("utm_", "pk_", "mtm_", "hsa_", "_bta_", "ga_")):
            tag, note = TRACK, "A campaign tracking tag."
        nested = None
        if val and (looks_like_url(val) and re.match(r"(https?|ftp)://|www\.", val, re.I)):
            nested = val
        elif val.startswith(("aHR0c", "aHR0cHM")):                 # base64 of "http"
            decoded = decode_b64(val)
            if decoded and looks_like_url(decoded):
                nested = decoded
                note = (note + " " if note else "") + "Base64 for a URL."
        if host.endswith("proofpoint.com") and low == "u" and val:
            fixed = unquote(re.sub(r"-([0-9A-F]{2})", r"%\1", val.replace("_", "/")))
            if looks_like_url(fixed):
                nested = fixed
                note = "Proofpoint URL Defense: your mail system rewrote the link. This is the original."
        if nested:
            tag = "url"
            if not note.startswith("Proofpoint"):
                note = ("Another address inside this one: after this site, you're probably sent on to it."
                        if low in REDIRECT else (note + " " if note else "") + "Another address inside this one.")
            note += " ⏎ inspects it."
            if low in REDIRECT:
                r.warn("Sends you on", f"The {name} parameter holds another address ({nested[:60]}"
                       f"{'…' if len(nested) > 60 else ''}). Where you end up is that site, not {host}.")
        elif low in SECRET or low.endswith(("_token", "token", "_key", "secret", "password")):
            tag = "secret"
            note = "Looks like a secret (a token, key, code or password). Anyone with the link can use it — " \
                   "don't share the link as it is."
            if JWT.fullmatch(val):
                note = jwt_note(val)
        elif JWT.fullmatch(val):
            tag, note = "secret", jwt_note(val)
        if EMAIL.search(val) and tag != "url":
            r.warn("Your email address", f"The {name} parameter contains an email address ({EMAIL.search(val)[0]}). "
                   "It tells the site who clicked, and anyone you forward the link to sees it.")
            tag = tag or "warn"
            note = note or "Contains an email address — likely the recipient's, to know who clicked."
        if not note and val and B64.fullmatch(val):
            dec = decode_b64(val)
            if dec:
                note = f"Base64-encoded: {dec[:200]}"
        if not note:
            note = "A parameter for the site. Its meaning is up to the site."
        r.add("Query", name or "(empty name)", val if eq else "(no value)", note, tag, url=nested)


def inspect_fragment(r, frag):
    text = unquote(frag)
    note = "The fragment: it stays in your browser and is never sent to the server. Usually it jumps to a " \
           "heading on the page."
    tag = ""
    if frag.startswith(":~:text="):
        note = "A text fragment: the browser scrolls to and highlights this text on the page."
        text = unquote(frag[len(":~:text="):])
    elif frag.startswith(("/", "!/")):
        note = "A route for a single-page app: the page's JavaScript reads it to decide what to show."
    elif re.search(r"(access_token|id_token|token)=", frag):
        note, tag = "Holds a login token (OAuth). Anyone with the full link can use it.", "secret"
        r.warn("Token in the link", "The part after # carries a login token.")
    r.add("Fragment", "fragment", text, note, tag)


def inspect_site(r, sp, host):
    """A word on a few sites' own link formats."""
    q = dict(urllib.parse.parse_qsl(sp.query))
    path = sp.path
    if host.endswith("youtube.com") and path == "/watch" and "v" in q:
        r.add("Site", "YouTube video", q["v"], "The video's ID. https://youtu.be/" + q["v"] + " is the short form."
              + (f" Starts at {q['t']}." if "t" in q else ""), "info")
    elif host == "youtu.be" and len(path) > 1:
        r.add("Site", "YouTube video", path[1:], "youtu.be is YouTube's own short-link domain; this is the video's ID.", "info")
    elif host.endswith("github.com"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            what = {"blob": "a file", "tree": "a folder", "issues": "an issue", "pull": "a pull request",
                    "commit": "a commit", "releases": "the releases", "actions": "the CI runs"}.get(parts[2] if len(parts) > 2 else "", "")
            r.add("Site", "GitHub repository", f"{parts[0]}/{parts[1]}", f"Owner {parts[0]}, repository {parts[1]}"
                  + (f"; this is {what}." if what else "."), "info")
    elif host.endswith("wikipedia.org") and path.startswith("/wiki/"):
        lang = host.split(".")[0]
        r.add("Site", "Wikipedia article", unquote(path[6:]).replace("_", " "), f"An article on the {lang} Wikipedia.", "info")
    elif re.search(r"(^|\.)google\.[a-z.]+$", host) and path == "/search" and "q" in q:
        r.add("Site", "Google search", q["q"], "A Google search for these words.", "info")
    elif re.search(r"(^|\.)google\.[a-z.]+$", host) and path == "/url" and ("q" in q or "url" in q):
        r.add("Site", "Google redirect", "", "Google's click-tracking redirect: it logs the click and sends you on.", "info")
    elif re.search(r"(^|\.)amazon\.[a-z.]+$", host):
        m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", path)
        if m:
            r.add("Site", "Amazon product", m[1], f"The product's ASIN. https://{host}/dp/{m[1]} is the whole link "
                  "without the tracking.", "info")
    elif host.endswith("safelinks.protection.outlook.com"):
        r.add("Site", "Outlook Safe Links", "", "Microsoft Defender rewrote this link in an email to check it when "
              "clicked. The url parameter is the original.", "info")


def inspect_opaque(r, url, scheme):
    """Schemes without //host: mailto, tel, data, javascript, magnet …"""
    rest = url[len(scheme) + 1:]
    r.segments += [(url[:len(scheme) + 1], "scheme"), (rest, "path")]
    r.add("Scheme", "scheme", scheme, SCHEMES.get(scheme, f"Opens whatever app handles {scheme}: links — a custom "
          "scheme the operating system hands to that app."))
    if scheme == "mailto":
        to, _, query = rest.partition("?")
        r.add("Mail", "to", unquote(to), "Who the message is addressed to.")
        for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
            r.add("Mail", k.lower(), v, {"subject": "The subject, filled in.", "body": "The message, filled in.",
                                         "cc": "Copied to.", "bcc": "Copied to, hidden."}.get(k.lower(), "A header."))
        r.summary = f"Opens a new email to {unquote(to) or 'nobody yet'}."
    elif scheme in ("tel", "sms"):
        number, _, query = rest.partition("?")
        r.add("Phone", "number", unquote(number), "The number." + (" A premium-rate number costs money to call." if
              re.match(r"\+?(0900|0906|0909|1900)", unquote(number)) else ""))
        r.summary = f"{'Calls' if scheme == 'tel' else 'Texts'} {unquote(number)}."
    elif scheme == "data":
        head, _, body = rest.partition(",")
        mime = head.split(";")[0] or "text/plain"
        b64 = ";base64" in head
        size = len(body) * 3 // 4 if b64 else len(unquote(body))
        r.add("Data", "type", mime, "What the content is.")
        r.add("Data", "encoding", "base64" if b64 else "percent-encoded text", "How it's written in the URL.")
        r.add("Data", "size", f"{size:,} bytes", "How much content is packed in.")
        preview = ""
        if b64:
            try:
                preview = base64.b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
            except (ValueError, binascii.Error, UnicodeDecodeError):
                preview = ""
        else:
            preview = unquote(body)
        if preview and mime.startswith("text"):
            r.add("Data", "content", preview[:2000], "The start of the content.")
        if mime in ("text/html", "application/xhtml+xml", "image/svg+xml", "text/javascript", "application/javascript"):
            r.warn("Page without an address", f"A data: URL with {mime} is a whole page or script in the link itself. "
                   "Browsers block opening these from links because they're used for phishing.")
        r.summary = f"Content inside the link itself: {mime}, {size:,} bytes. No server is contacted."
    elif scheme == "javascript":
        r.add("Code", "script", unquote(rest), "The JavaScript that would run.", "warn")
        r.warn("Runs code", "Clicking or pasting this runs JavaScript in the page you're on.")
        r.summary = "Runs JavaScript in the current page."
    elif scheme == "magnet":
        for k, v in urllib.parse.parse_qsl(rest.lstrip("?"), keep_blank_values=True):
            note = {"xt": "The exact topic: the torrent's info hash, which identifies the files.",
                    "dn": "The display name.", "tr": "A tracker that helps find peers.",
                    "xl": "The size in bytes.", "ws": "A web seed: a plain download location.",
                    "as": "An acceptable source.", "kt": "Keywords."}.get(k, "A magnet parameter.")
            r.add("Magnet", k, v, note, "url" if k == "ws" else "", url=v if k == "ws" else None)
        r.summary = "A torrent: your torrent client downloads it from other peers."
    else:
        r.add("Content", "rest", unquote(rest), "What the app receives.")
        r.summary = f"Handled by the app registered for {scheme}: links."
    return r


def clean_url(sp):
    """The URL without tracking parameters, keeping the rest exactly as written."""
    kept = []
    for pair in sp.query.split("&") if sp.query else []:
        name = unquote(pair.partition("=")[0]).lower()
        if PARAMS.get(name, ("",))[0] == TRACK or name.startswith(("utm_", "pk_", "mtm_", "hsa_")):
            continue
        kept.append(pair)
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, "&".join(kept), sp.fragment))


def summarise(r, sp, host):
    if not host:
        return "A link without a host."
    if "xn--" in host:
        try:
            host = host.encode("ascii").decode("idna")
        except UnicodeError:
            pass
    sub, domain, _ = registered_domain(host)
    name = site_name(domain)
    who = f"{domain} ({name})" if name else domain
    secure = {"https": "An encrypted link", "http": "An unencrypted link", "wss": "An encrypted WebSocket",
              "ws": "An unencrypted WebSocket"}.get(sp.scheme, f"A {sp.scheme} link")
    path = unquote(sp.path)
    where = "the home page" if path in ("", "/") else f"“{path if len(path) <= 50 else path[:49] + '…'}”"
    out = f"{secure} to {where} on {who}."
    query = [p for p in r.parts if p.section == "Query"]
    if query:
        track = sum(1 for p in query if p.tag == TRACK)
        secret = sum(1 for p in query if p.tag == "secret")
        bits = [f"{track} for tracking" if track else "", f"{secret} that look{'s' if secret == 1 else ''} secret" if secret else "",
                "one holding another address" if any(p.url for p in query) else ""]
        bits = [b for b in bits if b]
        out += f" It passes {len(query)} parameter{'s' if len(query) != 1 else ''}" + \
               (f" ({', '.join(bits)})" if bits else "") + "."
    if sp.fragment:
        out += " The part after # stays in your browser."
    return out


# -- following redirects --

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def follow(url):
    """[(status, url, note)] from url to wherever the redirects end."""
    opener = urllib.request.build_opener(NoRedirect)
    hops, seen = [], set()
    for _ in range(MAX_HOPS):
        if url in seen:
            hops.append((0, url, "loops back to an earlier address"))
            break
        seen.add(url)
        if not url.lower().startswith(("http://", "https://")):
            hops.append((0, url, "not a web address — not followed"))
            break
        status, location, note = None, None, ""
        for method in ("HEAD", "GET"):
            req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
            try:
                with opener.open(req, timeout=TIMEOUT) as resp:
                    status = resp.status
            except urllib.error.HTTPError as e:
                status, location = e.code, e.headers.get("Location")
                e.close()
            except (urllib.error.URLError, OSError, ValueError) as e:
                reason = getattr(e, "reason", e)
                hops.append((0, url, f"failed: {reason}"))
                return hops
            if status not in (403, 405, 501) or method == "GET":
                break
        if 300 <= status < 400 and location:
            nxt = urllib.parse.urljoin(url, location)
            hops.append((status, url, f"redirects ({status})"))
            url = nxt
            continue
        note = "the end" if status < 400 else f"the server answered {status}"
        hops.append((status, url, note))
        break
    else:
        hops.append((0, url, f"stopped after {MAX_HOPS} redirects"))
    return hops


# -- the app --

def cwidth(text):
    if text.isascii():
        return len(text)
    width = 0
    for ch in text:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def fit(text, width):
    """Clip with an ellipsis and pad to exactly `width` display columns."""
    if width <= 0:
        return ""
    if cwidth(text) > width:
        kept, used = [], 0
        for ch in text:
            w = cwidth(ch)
            if used + w > width - 1:
                break
            kept.append(ch)
            used += w
        text = "".join(kept) + "…"
    return text + " " * (width - cwidth(text))


def wrap(text, width):
    out = []
    for para in text.split("\n"):
        words, line = para.split(" "), ""
        for word in words:
            while cwidth(word) > width:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:width])
                word = word[width:]
            if line and cwidth(line) + 1 + cwidth(word) > width:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        out.append(line)
    return out


def clipboard():
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


TAGS = {"warn": ("⚠", 4), "secret": ("secret", 4), TRACK: ("tracking", 3), "url": ("URL ⏎", 6),
        "key": ("◀ check", 2), "info": ("", 0), "dim": ("", 5), "": ("", 0)}
SEG_COLORS = {"scheme": 1, "user": 4, "host": 2, "port": 3, "path": 0, "punct": 5, "key": 7, "value": 0, "fragment": 6}

HELP = [
    ("↑↓ jk  PgUp PgDn", "move through the parts"),
    ("⏎", "inspect the URL inside a parameter, or the end of the redirects"),
    ("Backspace", "back to the URL you came from"),
    ("i", "type or paste a new URL"),
    ("v", "inspect the URL on the clipboard"),
    ("r", "follow the redirects (contacts the server with HEAD requests, opens nothing)"),
    ("y", "copy the selected value"),
    ("Y", "copy the whole URL"),
    ("c", "copy the clean link — without tracking parameters"),
    ("o", "open the URL in the browser"),
    ("q", "quit"),
]


class App:
    def __init__(self, stdscr, url):
        self.stdscr = stdscr
        self.history = []
        self.report = None
        self.sel = 0
        self.top = 0
        self.help = False
        self.busy = None               # thread following redirects
        self.result = None
        self.message, self.message_at, self.message_attr = "", 0.0, 0
        if url:
            self.load(url)

    def load(self, url, remember=False):
        if remember and self.report:
            self.history.append((self.report, self.sel))
        self.report = inspect(url)
        self.sel, self.top = 0, 0

    def rows(self):
        r = self.report
        if not r:
            return []
        rows = []
        if r.warnings:
            rows += r.warnings
        rows += r.parts
        if r.clean:
            rows.append(Part("Clean link", "without tracking", r.clean, "The same link with the tracking parameters "
                             "removed. c copies it.", "info"))
        if r.hops:
            for k, (status, url, note) in enumerate(r.hops, 1):
                last = k == len(r.hops)
                rows.append(Part("Redirects", f"{k}. {status or '—'}", url, f"Hop {k}: {note}."
                                 + (" ⏎ inspects where it ends up." if last and k > 1 else ""),
                                 "url" if last and k > 1 else ("warn" if status == 0 else "dim"),
                                 url=url if last and k > 1 else None))
        return rows

    def say(self, text, attr=None):
        self.message, self.message_at = text, time.monotonic()
        self.message_attr = curses.color_pair(3) if attr is None else attr

    def put(self, y, x, text, attr=0, width=None):
        h, w = self.stdscr.getmaxyx()
        width = w - x if width is None else min(width, w - x)
        if width <= 0 or y < 0 or y >= h or x < 0:
            return
        try:
            self.stdscr.addstr(y, x, fit(text, width), attr)
        except curses.error:
            pass

    # -- drawing --

    def draw(self):
        s = self.stdscr
        s.erase()
        h, w = s.getmaxyx()
        if w < MIN_W or h < MIN_H:
            self.put(0, 0, f"window too small — needs {MIN_W}×{MIN_H}", curses.color_pair(4))
            s.refresh()
            return
        self.put(0, 0, " URL inspector ", curses.color_pair(1) | curses.A_BOLD | curses.A_REVERSE)
        r = self.report
        if r and r.warnings:
            label = f"⚠ {len(r.warnings)} warning{'s' if len(r.warnings) != 1 else ''}"
            self.put(0, w - cwidth(label) - 1, label, curses.color_pair(4) | curses.A_BOLD)
        if self.history:
            self.put(0, 17, f"nested {len(self.history)} deep — ⌫ back", curses.color_pair(5))
        if self.help:
            self.draw_help(h, w)
        elif not r:
            self.put(2, 2, "No URL yet.", curses.A_BOLD)
            self.put(3, 2, "i  type or paste one", curses.color_pair(5))
            self.put(4, 2, "v  take the one on the clipboard", curses.color_pair(5))
        else:
            self.draw_report(h, w)
        self.draw_footer(h - 1, w)
        s.refresh()

    def draw_url(self, y, w):
        """The URL in colour, wrapped over at most URL_LINES lines."""
        x, line, room = 2, 0, w - 4
        for text, kind in self.report.segments:
            attr = curses.color_pair(SEG_COLORS.get(kind, 0)) | (curses.A_BOLD if kind in ("host", "key") else 0)
            while text:
                take, used = 0, 0
                for ch in text:
                    cw = cwidth(ch)
                    if x - 2 + used + cw > room:
                        break
                    used += cw
                    take += 1
                if take:
                    self.put(y + line, x, text[:take], attr, used)
                    x += used
                    text = text[take:]
                if text:
                    line += 1
                    x = 2
                    if line >= URL_LINES:
                        self.put(y + line - 1, w - 3, "…", curses.color_pair(5))
                        return y + line
        return y + line + 1

    def draw_report(self, h, w):
        r = self.report
        y = self.draw_url(2, w)
        summary = wrap(r.summary, w - 4)[:3]
        for k, line in enumerate(summary):
            self.put(y + 1 + k, 2, line, curses.A_BOLD if k == 0 else 0)
        y += 2 + len(summary)
        rows = self.rows()
        self.sel = max(0, min(self.sel, len(rows) - 1))
        detail = wrap(rows[self.sel].note, w - 6) if rows else []
        detail_h = min(len(detail), 6) + 1
        list_h = h - 1 - y - detail_h - 1
        if list_h < 3:
            list_h, detail = h - 1 - y - 1, []
        label_w = min(22, max(cwidth(p.label) for p in rows) + 2) if rows else 10
        tag_w = 10
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + list_h:
            self.top = self.sel - list_h + 1
        # section headers interleave with the rows; walk from the top to place them
        lines, last = [], None
        for i, p in enumerate(rows):
            if p.section != last:
                lines.append((None, p.section))
                last = p.section
            lines.append((i, p))
        at = next((k for k, (i, _) in enumerate(lines) if i == self.sel), 0)
        start = max(0, min(getattr(self, "line_top", 0), len(lines) - list_h))
        if at < start:
            start = max(0, at - 1)
        elif at >= start + list_h:
            start = at - list_h + 1
        self.line_top = start
        for k, (i, p) in enumerate(lines[start:start + list_h]):
            yy = y + k
            if i is None:
                color = 4 if p == "Warnings" else 1
                self.put(yy, 1, p, curses.color_pair(color) | curses.A_BOLD)
                continue
            here = i == self.sel
            base = curses.A_REVERSE if here else 0
            tag, color = TAGS.get(p.tag, ("", 0))
            self.put(yy, 0, " " * w, base)
            if p.section == "Warnings":
                self.put(yy, 3, "⚠ " + p.label, base | curses.color_pair(4) | curses.A_BOLD, w - 4)
                continue
            self.put(yy, 3, p.label, base | curses.color_pair(5), label_w)
            value_attr = base | (curses.color_pair(5) if p.tag == "dim" else 0) | (curses.A_BOLD if p.tag == "key" else 0)
            self.put(yy, 3 + label_w, p.value.replace("\n", "↵"), value_attr, w - 5 - label_w - tag_w)
            if tag:
                self.put(yy, w - tag_w - 1, tag.rjust(tag_w), base | curses.color_pair(color) | curses.A_BOLD, tag_w)
        if detail:
            dy = h - 1 - detail_h
            self.put(dy, 1, "─" * (w - 2), curses.color_pair(5))
            for k, line in enumerate(detail[:detail_h - 1]):
                self.put(dy + 1 + k, 3, line)

    def draw_help(self, h, w):
        for k, (keys, what) in enumerate(HELP):
            self.put(2 + k, 3, keys, curses.A_BOLD, 18)
            self.put(2 + k, 22, what, curses.color_pair(5))

    def draw_footer(self, y, w):
        if self.busy:
            self.put(y, 0, " following redirects…", curses.color_pair(3))
            return
        if self.message and time.monotonic() - self.message_at < MESSAGE_SECONDS:
            self.put(y, 0, " " + self.message, self.message_attr)
            return
        keys = ["↑↓ parts", "⏎ inspect", "r redirects", "c clean", "y copy", "i new", "v paste", "⌫ back", "? help",
                "q quit"] if not self.help else ["? back", "q quit"]
        while len(keys) > 2 and cwidth("  ".join(keys)) > w - 2:
            keys.pop(-2)
        self.put(y, 0, " " + "  ".join(keys), curses.color_pair(5))

    # -- actions --

    def prompt(self, label):
        curses.curs_set(1)
        buf = []
        try:
            while True:
                h, w = self.stdscr.getmaxyx()
                room = w - cwidth(label) - 2
                shown = "".join(buf)[-room:] if room > 0 else ""
                text = label + shown
                self.put(h - 1, 0, text, curses.color_pair(1) | curses.A_REVERSE)
                self.stdscr.move(h - 1, min(cwidth(text), w - 2))
                self.stdscr.refresh()
                try:
                    key = self.stdscr.get_wch()
                except curses.error:
                    continue
                if isinstance(key, str):
                    if key in ("\n", "\r"):
                        return "".join(buf)
                    if key == "\x1b":
                        return None
                    if key in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif key == "\x15":
                        buf.clear()
                    elif key.isprintable():
                        buf.append(key)
                elif key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    if buf:
                        buf.pop()
                elif key == curses.KEY_ENTER:
                    return "".join(buf)
        finally:
            curses.curs_set(0)

    def copy(self, text, what):
        try:
            subprocess.run(["pbcopy"], input=text.encode(), check=True)
            self.say(f"copied {what}")
        except (OSError, subprocess.CalledProcessError):
            self.say("couldn't copy — pbcopy not found", curses.color_pair(4))

    def new_url(self, text, remember=False):
        text = (text or "").strip()
        if not text:
            return
        if not looks_like_url(text) and "." not in text:
            self.say("that doesn't look like a URL", curses.color_pair(4))
            return
        self.load(text, remember)

    def start_follow(self):
        r = self.report
        if not r or not r.url.lower().startswith(("http://", "https://")):
            self.say("only http and https links can be followed", curses.color_pair(4))
            return

        def work():
            self.result = (r, follow(r.url))
        self.busy = threading.Thread(target=work, daemon=True)
        self.busy.start()

    def poll(self):
        if self.busy and not self.busy.is_alive():
            self.busy = None
            report, hops = self.result
            report.hops = hops
            if report is self.report:
                self.sel = len(self.rows()) - 1
            end = hops[-1]
            self.say(f"{len(hops) - 1} redirect{'s' if len(hops) != 2 else ''} — ends at {end[1]}" if len(hops) > 1
                     else f"no redirects: {end[2]}")

    def handle(self, ch):
        rows = self.rows()
        if self.help:
            if ch in (ord("?"), ord("q"), 27, 10, 13, curses.KEY_BACKSPACE, 127):
                self.help = False
            return True
        steps = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_PPAGE: -10,
                 curses.KEY_NPAGE: 10, ord("g"): -10**6, curses.KEY_HOME: -10**6, ord("G"): 10**6, curses.KEY_END: 10**6}
        if ch in (ord("q"), ord("Q")):
            return False
        if ch in steps and rows:
            self.sel = max(0, min(len(rows) - 1, self.sel + steps[ch]))
        elif ch in (10, 13, curses.KEY_ENTER) and rows:
            p = rows[self.sel]
            if p.url:
                self.load(p.url, remember=True)
            else:
                self.say("nothing to open here — rows marked URL ⏎ hold another address")
        elif ch in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT):
            if self.history:
                self.report, self.sel = self.history.pop()
        elif ch == ord("i"):
            self.new_url(self.prompt(" URL: "), remember=True)
        elif ch == ord("v"):
            text = clipboard()
            if text:
                self.new_url(text, remember=True)
            else:
                self.say("the clipboard is empty", curses.color_pair(4))
        elif ch == ord("y") and rows:
            p = rows[self.sel]
            self.copy(p.value or p.label, p.label)
        elif ch == ord("Y") and self.report:
            self.copy(self.report.url, "the URL")
        elif ch == ord("c") and self.report:
            if self.report.clean:
                self.copy(self.report.clean, "the clean link")
            else:
                self.say("nothing to clean — no tracking parameters")
        elif ch == ord("r") and not self.busy:
            self.start_follow()
        elif ch == ord("o") and self.report:
            try:
                subprocess.run(["open", self.report.url], check=True, capture_output=True)
                self.say("opened in the browser")
            except (OSError, subprocess.CalledProcessError):
                self.say("couldn't open it", curses.color_pair(4))
        elif ch == ord("?"):
            self.help = True
        return True


def run(stdscr, url):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title, scheme, sections
    curses.init_pair(2, curses.COLOR_GREEN, -1)    # host
    curses.init_pair(3, curses.COLOR_YELLOW, -1)   # port, tracking, messages
    curses.init_pair(4, curses.COLOR_RED, -1)      # warnings, secrets
    curses.init_pair(5, 8 if curses.COLORS > 8 else curses.COLOR_WHITE, -1)   # dim
    curses.init_pair(6, curses.COLOR_BLUE, -1)     # fragment, nested URLs
    curses.init_pair(7, curses.COLOR_MAGENTA, -1)  # query keys
    stdscr.keypad(True)
    app = App(stdscr, url)
    while True:
        stdscr.timeout(TICK_MS if app.busy else -1)
        app.poll()
        app.draw()
        ch = stdscr.getch()
        if ch in (-1, curses.KEY_RESIZE):
            continue
        if not app.handle(ch):
            return


def print_once(url, follow_redirects):
    r = inspect(url)
    if follow_redirects and r.url.lower().startswith(("http://", "https://")):
        r.hops = follow(r.url)
    tty = sys.stdout.isatty()
    bold, dim, red, cyan, reset = ("\033[1m", "\033[2m", "\033[31m", "\033[36m", "\033[0m") if tty else ("",) * 5
    print(f"{bold}{r.url}{reset}\n")
    for line in wrap(r.summary, 90):
        print(line)
    width = 90
    if r.warnings:
        print(f"\n{red}{bold}Warnings{reset}")
        for p in r.warnings:
            print(f"  {red}⚠ {p.label}{reset}")
            for line in wrap(p.note, width - 4):
                print(f"    {dim}{line}{reset}")
    last = None
    for p in r.parts:
        if p.section != last:
            print(f"\n{cyan}{bold}{p.section}{reset}")
            last = p.section
        tag = TAGS.get(p.tag, ("", 0))[0]
        print(f"  {p.label:<18} {p.value}" + (f"  {red if p.tag in ('warn', 'secret') else ''}[{tag}]{reset}" if tag else ""))
        for line in wrap(p.note, width - 21):
            print(f"  {'':<18} {dim}{line}{reset}")
    if r.clean:
        print(f"\n{cyan}{bold}Clean link{reset}\n  {r.clean}")
    if r.hops:
        print(f"\n{cyan}{bold}Redirects{reset}")
        for k, (status, hop, note) in enumerate(r.hops, 1):
            print(f"  {k}. {status or '—':>3}  {hop}  {dim}{note}{reset}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Take a URL apart and explain each piece: host, path, parameters, tracking, warnings.")
    parser.add_argument("url", nargs="?", help="the URL (default: the one on the clipboard, or standard input)")
    parser.add_argument("--once", action="store_true", help="print the breakdown and exit")
    parser.add_argument("--follow", action="store_true", help="with --once, also follow the redirects (uses the network)")
    args = parser.parse_args()
    url = args.url
    if url == "-" or (url is None and not sys.stdin.isatty()):
        url = sys.stdin.read().strip()
        if not args.once:
            os.dup2(os.open("/dev/tty", os.O_RDONLY), 0)
    if url is None:
        text = clipboard()
        url = text if looks_like_url(text) else None
    if args.once:
        if not url:
            parser.error("no URL given, and none on the clipboard")
        raise SystemExit(print_once(url, args.follow))
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    try:
        curses.wrapper(run, url)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
