# Plano — Serviço de Download Debrid Personalizado

## Objetivo

Substituir o RDT-Client por um serviço próprio que:
1. Se liga ao Sonarr/Radarr exatamente como um qBittorrent real (drop-in replacement, sem reconfigurar o Sonarr/Radarr além do host/porta).
2. Fala com o provider de debrid (TorBox, extensível a Real-Debrid depois).
3. Descarrega os ficheiros com **múltiplas conexões HTTP paralelas** (range requests), para se aproximar da velocidade real do browser — resolvendo o teto de ~3-8MB/s identificado no RDT-Client (Aria2c e Bezzad Downloader, ambos presos a `connections: 1` mesmo com paralelismo configurado).

## Contexto / Motivação (o que já validámos)

- Testado e confirmado, via `curl` manual com múltiplas conexões `Range` em paralelo: tanto o CDN do TorBox como o do Real-Debrid **aceitam e aceleram genuinamente** com múltiplas conexões (4 conexões → ~4x throughput agregado, ex: 4×5.8MB/s ≈ 23MB/s no TorBox, 4×7.4MB/s ≈ 29.6MB/s no Real-Debrid).
- O RDT-Client, testado com dois downloaders internos diferentes (Aria2c via RPC e Bezzad Downloader nativo .NET), fica sempre preso a 1 conexão efetiva por ficheiro, independentemente da configuração de `split`/`max-connection-per-server`/`Parallel connections`/`Chunk Count`.
- Isto é um problema conhecido e documentado em issues do próprio repositório (ex: issue #216 — downloader "antigo" do RDT-Client era mais rápido que os atuais, mas foi descontinuado).
- Fork alternativo (Pukabyte/rdtclient) investigado — imagem Docker desatualizada há ~2 anos, não é candidato viável.
- Conclusão: vale a pena construir um serviço mínimo e próprio, focado só em resolver isto bem.

## Arquitetura

```
Sonarr/Radarr
     │  (fala "qBittorrent" via API HTTP)
     ▼
┌─────────────────────────────────────────┐
│         Nosso Serviço (FastAPI)          │
│                                           │
│  ┌───────────────┐   ┌─────────────────┐ │
│  │ API qBittorrent│   │   Dashboard Web  │ │
│  │   (fachada)    │   │  (settings, UI)  │ │
│  └───────┬───────┘   └─────────────────┘ │
│          │                                │
│  ┌───────▼────────┐                      │
│  │  Download Model │  (estado interno)    │
│  │  (SQLite)        │                      │
│  └───────┬────────┘                      │
│          │                                │
│  ┌───────▼────────┐   ┌─────────────────┐ │
│  │ Provider Client │   │ Downloader multi-│ │
│  │  (TorBox API)   │──▶│  conexão (aiohttp)│ │
│  └────────────────┘   └────────┬────────┘ │
└─────────────────────────────────┼──────────┘
                                   ▼
                          /torbox/<categoria>/...
```

## Stack técnica

| Componente | Escolha | Porquê |
|---|---|---|
| Linguagem | Python 3.12 | Rápido de iterar, boas libs async |
| Framework web | FastAPI | Async nativo, fácil de testar, docs automáticas |
| HTTP client (downloads) | `aiohttp` | Async, eficiente para muitas conexões paralelas em I/O-bound |
| Escrita em disco | `aiofiles` + `seek()` por offset | Cada chunk escreve diretamente na posição certa, sem montagem posterior |
| Dashboard | HTML + Jinja2, servido pelo próprio FastAPI | Sem necessidade de build step / framework JS para o MVP |
| Persistência | SQLite via `aiosqlite` | Ficheiro simples, sem container de BD extra |
| Deploy | Docker, integrado no `docker-compose.yml` existente | Mesma rede Docker que Sonarr/Radarr/Aria2 já usam |

## Modelo de dados (rascunho)

```python
class Download:
    hash: str                # identificador único (usado pelo Sonarr)
    name: str
    category: str             # "tv-sonarr", "radarr", etc.
    provider_torrent_id: str  # ID no TorBox
    state: str                 # error | pausedUP | stalledDL | downloading
    total_size: int
    downloaded_bytes: int      # agregado, atualizado pelas conexões paralelas em tempo real
    download_speed: float      # média deslizante (janela ~3s)
    save_path: str
    content_path: str
    files: list[FileInfo]      # suporte a season packs / múltiplos ficheiros
    added_on: datetime
    completed_on: datetime | None
    error_message: str | None

class FileInfo:
    name: str
    size: int
    downloaded_bytes: int
    selected: bool
    provider_download_url: str | None  # link direto, obtido do provider quando cached
```

## Especificação da API qBittorrent a implementar

(mapeamento completo já extraído do código-fonte real do RDT-Client — ver `qbittorrent-api-spec.md`)

**Essenciais (Fase 3):**
- `POST /api/v2/auth/login`
- `GET /api/v2/app/version`, `webapiVersion`, `preferences`, `buildInfo`, `defaultSavePath`
- `GET /api/v2/torrents/info?category=X`
- `GET /api/v2/torrents/files?hash=X`
- `GET /api/v2/torrents/properties?hash=X`
- `POST /api/v2/torrents/add`
- `POST /api/v2/torrents/delete`
- `POST /api/v2/torrents/pause` / `resume`
- `GET/POST /api/v2/torrents/categories`, `createCategory`, `setCategory`
- `GET /api/v2/transfer/info`
- `GET /api/v2/sync/maindata`

**Stub (200 OK, no-op):**
`app/shutdown`, `app/setPreferences`, `torrents/setShareLimits`, `torrents/filePrio`, `torrents/createTags`, `torrents/tags`

**Mapeamento de estados:**
1. erro → `"error"`
2. completo → `"pausedUP"` (Sonarr interpreta como pronto a importar)
3. downloading sem seeders no provider → `"stalledDL"`
4. caso contrário → `"downloading"`

**Fórmula de progresso:** `(progresso_provider + progresso_download_local) / 2.0`

## Estratégia de otimização de velocidade (o core do projeto)

1. `HEAD` request ao link direto do provider → obter `Content-Length` e confirmar suporte a `Range` (`Accept-Ranges: bytes`).
2. Dividir o ficheiro em N partes iguais (N configurável, default 8-16).
3. Abrir N tasks `asyncio` em paralelo, cada uma:
   - Faz `GET` com header `Range: bytes=<início>-<fim>`
   - Escreve o conteúdo recebido diretamente no ficheiro final, na posição certa (`seek(offset)`)
   - Reporta bytes recebidos ao contador agregado do `Download`
4. Retry automático por chunk (3 tentativas) — se uma conexão falhar a meio, só essa parte recomeça.
5. Cálculo de velocidade: média deslizante dos últimos ~3 segundos, somando todas as conexões ativas.
6. Configurável via dashboard: nº de conexões paralelas, tamanho mínimo de ficheiro para ativar paralelismo (ficheiros pequenos tipo .nfo não precisam).

## Fases de desenvolvimento

### Fase 1 — Downloader multi-conexão isolado (prioridade máxima, testar primeiro)
Script standalone, sem API nem dashboard. Dado um URL, descarrega com N conexões paralelas, escreve ficheiro final, mede e imprime velocidade real. Objetivo: confirmar programaticamente (não manualmente com curl) que conseguimos os ~20-30MB/s já provados.

### Fase 2 — Cliente TorBox
Módulo separado: autentica com API key, submete magnet, faz polling do estado até "cached", obtém lista de ficheiros e links diretos de download.

### Fase 3 — API qBittorrent (fachada)
Implementar os endpoints essenciais listados acima, primeiro com dados mock, depois ligados aos módulos reais (Fase 1 + Fase 2).

### Fase 4 — Dashboard
Interface web simples: lista de downloads com progresso em tempo real (polling ou SSE), página de settings (API key do provider, nº de conexões paralelas, paths).

### Fase 5 — Integração e testes fim-a-fim
Apontar o Sonarr/Radarr para este serviço (mesma categoria `tv-sonarr`/`radarr`), testar o fluxo completo: grab → add → download paralelo → completed → import automático.

### Fase 6 — Dockerizar
Criar `Dockerfile`, adicionar serviço ao `docker-compose.yml` existente, testar em paralelo com o RDT-Client atual antes de o substituir de vez.

## Fora do escopo do MVP

- Watch folders
- Múltiplos providers em simultâneo (só TorBox para já, extensível depois)
- Unpacking de RAR (assumimos que o provider já entrega ficheiros prontos)
- Symlink/rclone mount (fica para uma iteração futura, possivelmente já no Proxmox)
- Seed ratio / upload (campo fake, só para compatibilidade com o Sonarr)

## Critério de sucesso

Um download real via Sonarr, usando o nosso serviço, atinge velocidade agregada próxima da que já validámos manualmente (20-30MB/s+), e o Sonarr consegue detetar corretamente o progresso e importar o ficheiro automaticamente ao terminar — sem qualquer intervenção manual.
