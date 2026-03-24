"""
Extrae palabras desde nombres de funciones/métodos en Python y Java,
procesando repositorios ordenados por popularidad (stars desc).
Publica las palabras en Redis para el Visualizer.
"""

import os
import re
import time
import logging
import base64
import requests
import redis
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MINER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────
GITHUB_TOKEN        = os.getenv("GITHUB_TOKEN", "")
REDIS_HOST          = os.getenv("REDIS_HOST", "redis")
REDIS_PORT          = int(os.getenv("REDIS_PORT", 6379))
REDIS_CHANNEL       = "words"
REPOS_PER_PAGE      = int(os.getenv("REPOS_PER_PAGE", 10))
MAX_FILES_PER_REPO  = int(os.getenv("MAX_FILES_PER_REPO", 20))
SLEEP_BETWEEN_REPOS = float(os.getenv("SLEEP_BETWEEN_REPOS", 2))
SLEEP_BETWEEN_PAGES = float(os.getenv("SLEEP_BETWEEN_PAGES", 5))

# ── Sesión HTTP hacia GitHub ──────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})
if GITHUB_TOKEN:
    SESSION.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    log.info("Token de GitHub configurado → 5 000 req/hr disponibles.")
else:
    log.warning("Sin GITHUB_TOKEN → límite público de 60 req/hr.")


def gh_get(url: str, params: dict | None = None) -> dict | list | None:
    """GET con reintentos ante rate-limit (403/429)."""
    for attempt in range(4):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            if r.status_code in (403, 429):
                wait = int(r.headers.get("Retry-After", 60))
                log.warning("Rate limit alcanzado. Esperando %ds …", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            log.error("Error de red: %s. Intento %d/4.", exc, attempt + 1)
            time.sleep(5 * (attempt + 1))
    return None


def search_repos(language: str, page: int = 1) -> list[dict]:
    """Repositorios ordenados por stars descendente para un lenguaje dado."""
    data = gh_get(
        "https://api.github.com/search/repositories",
        params={
            "q": f"language:{language} stars:>100",
            "sort": "stars",
            "order": "desc",
            "per_page": REPOS_PER_PAGE,
            "page": page,
        },
    )
    return (data or {}).get("items", [])


def get_file_tree(owner: str, repo: str, language: str) -> list[str]:
    """URLs de blobs de archivos fuente del lenguaje dado."""
    ext = ".py" if language == "Python" else ".java"
    data = gh_get(
        f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD",
        params={"recursive": "1"},
    )
    if not data:
        return []
    urls = [
        item["url"]
        for item in data.get("tree", [])
        if item.get("type") == "blob" and item["path"].endswith(ext)
    ]
    return urls[:MAX_FILES_PER_REPO]


def fetch_blob(blob_url: str) -> str | None:
    """Descarga y decodifica un blob de GitHub, para asi poder leerlo."""
    data = gh_get(blob_url)
    if not data:
        return None
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return None
    return data.get("content")


# ── Separación de identificadores ─────────────────────────────────────────────
_SNAKE_RE  = re.compile(r"[_\s]+")
_CAMEL_RE  = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALPHA = re.compile(r"[^a-zA-Z]")


def split_identifier(name: str) -> list[str]:
    """
    Divide un identificador en palabras individuales.
    Soporta snake_case, camelCase, PascalCase y combinaciones.
    """
    words = []
    for part in _SNAKE_RE.split(name):
        for sub in _CAMEL_RE.sub(" ", part).split():
            clean = _NON_ALPHA.sub("", sub).lower()
            if len(clean) > 1:
                words.append(clean)
    return words


# ── Extractores por lenguaje ──────────────────────────────────────────────────
_PYTHON_FUNC_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_JAVA_METHOD_RE = re.compile(
    r"(?:public|private|protected|static|final|synchronized|abstract|native|default)"
    r"[\w\s<>\[\]?,]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_JAVA_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "return", "new",
    "class", "interface", "enum", "try", "catch", "finally",
})


def extract_words_python(source: str) -> list[str]:
    words = []
    for m in _PYTHON_FUNC_RE.finditer(source):
        name = m.group(1).strip("_")   # quitar dunders
        words.extend(split_identifier(name))
    return words


def extract_words_java(source: str) -> list[str]:
    words = []
    for m in _JAVA_METHOD_RE.finditer(source):
        name = m.group(1)
        if name in _JAVA_KEYWORDS:
            continue
        words.extend(split_identifier(name))
    return words


EXTRACTORS = {
    "Python": extract_words_python,
    "Java":   extract_words_java,
}


# ── Conexión Redis ────────────────────────────────────────────────────────────
def connect_redis() -> redis.Redis:
    while True:
        try:
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
            r.ping()
            log.info("Conectado a Redis en %s:%d", REDIS_HOST, REDIS_PORT)
            return r
        except redis.ConnectionError:
            log.warning("Redis no disponible, reintentando en 3s …")
            time.sleep(3)


def publish_words(r: redis.Redis, words: list[str], meta: dict) -> None:
    """Publica palabras en pub/sub y acumula conteos en un sorted set."""
    if not words:
        return
    r.publish(REDIS_CHANNEL, json.dumps({"words": words, "meta": meta}))
    pipe = r.pipeline()
    for w in words:
        pipe.zincrby("word_counts", 1, w)
    pipe.execute()


# ── Loop principal de minería ─────────────────────────────────────────────────
def mine_language(r: redis.Redis, language: str):
    """Generador infinito: mina repos de un lenguaje, página por página."""
    page = 1
    while True:
        log.info("[%s] Buscando repos — página %d …", language, page)
        repos = search_repos(language, page)
        if not repos:
            log.info("[%s] Sin resultados en página %d, volviendo a página 1.", language, page)
            page = 1
            time.sleep(SLEEP_BETWEEN_PAGES)
            continue

        for repo in repos:
            owner = repo["owner"]["login"]
            name  = repo["name"]
            stars = repo["stargazers_count"]
            log.info("[%s] Minando %s/%s (%d ⭐) …", language, owner, name, stars)

            file_urls = get_file_tree(owner, name, language)
            if not file_urls:
                log.info("  Sin archivos %s encontrados.", language)
                time.sleep(SLEEP_BETWEEN_REPOS)
                yield
                continue

            total = 0
            for blob_url in file_urls:
                source = fetch_blob(blob_url)
                if source is None:
                    continue
                words = EXTRACTORS[language](source)
                if words:
                    publish_words(r, words, {
                        "repo":     f"{owner}/{name}",
                        "stars":    stars,
                        "language": language,
                    })
                    total += len(words)
                time.sleep(0.3)

            log.info("  → %d palabras publicadas desde %s/%s.", total, owner, name)
            time.sleep(SLEEP_BETWEEN_REPOS)
            yield

        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)


def main() -> None:
    r = connect_redis()
    log.info("Miner iniciado.")

    python_gen = mine_language(r, "Python")
    java_gen   = mine_language(r, "Java")

    while True:
        next(python_gen)
        next(java_gen)


if __name__ == "__main__":
    main()
