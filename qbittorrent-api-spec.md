# Especificação da API qBittorrent (v4.3.2 / WebUI 2.7)
Baseado no código fonte real do RDT-Client (server/RdtClient.Web/Controllers/QBittorrentController.cs)
Esta é a API que o Sonarr e Radarr esperam de um "qBittorrent" — vamos implementar exatamente isto.

## Endpoints que TEMOS de implementar (usados ativamente pelo Sonarr/Radarr)

### Autenticação
- `POST /api/v2/auth/login` — form: username, password → resposta texto "Ok." ou "Fails."
- `POST /api/v2/auth/logout` — termina sessão

### Info da app (chamados no handshake, sempre antes de cada poll)
- `GET /api/v2/app/version` → "v4.3.2"
- `GET /api/v2/app/webapiVersion` → "2.7"
- `GET /api/v2/app/buildInfo` → objeto com versões fictícias (Boost, libtorrent, etc.)
- `GET /api/v2/app/preferences` → objeto grande, maioria hardcoded; só save_path, temp_path, web_ui_username são reais
- `GET /api/v2/app/defaultSavePath` → string do path

### Listagem/estado de torrents (POLLING PRINCIPAL)
- `GET /api/v2/torrents/info?category=X` → array de TorrentInfo
  - Campos: hash, name, size, progress (0.0-1.0), dlspeed, upspeed, state, category,
    save_path, content_path, completed, amount_left, completion_on, added_on
  - progress = média entre progresso do provider e progresso local do download
- `GET /api/v2/torrents/files?hash=X` → lista de ficheiros do torrent (só os selected=true)
- `GET /api/v2/torrents/properties?hash=X` → detalhes: total_size, total_downloaded,
  dl_speed, dl_speed_avg, pieces_num, pieces_have, time_elapsed, etc.

### Gestão (comandos)
- `POST /api/v2/torrents/add` — form: urls (magnet/http, newline-separated), category, priority
  - Erro esperado se debrid rejeitar (ex: DMCA) → devolver "Fails."
- `POST /api/v2/torrents/delete` — form: hashes (pipe-separated "h1|h2"), deleteFiles (bool)
- `POST /api/v2/torrents/pause` — form: hashes
- `POST /api/v2/torrents/resume` — form: hashes
- `POST /api/v2/torrents/topPrio` — form: hashes (definir prioridade máxima)

### Categorias
- `GET /api/v2/torrents/categories` → dict nome→objeto categoria
- `POST /api/v2/torrents/createCategory` — form: category
- `POST /api/v2/torrents/removeCategories` — form: categories (newline-separated)
- `POST /api/v2/torrents/setCategory` — form: hashes, category

### Transfer/Sync
- `GET /api/v2/transfer/info` → connection_status, dl_info_data, dl_info_speed, dl_rate_limit
- `GET /api/v2/sync/maindata` → snapshot completo: categories + torrents + server_state
  (full_update sempre true, não suportamos updates incrementais — mais simples para nós também)

## Endpoints STUB (aceitar e devolver 200 OK, sem fazer nada)
Necessários só para não dar erro se algum cliente os chamar durante init:
- `POST /api/v2/app/shutdown`
- `POST /api/v2/app/setPreferences`
- `POST /api/v2/torrents/setShareLimits`
- `POST /api/v2/torrents/filePrio`
- `POST /api/v2/torrents/createTags`
- `GET /api/v2/torrents/tags` → []

## MAPEAMENTO DE ESTADOS (crítico para o Sonarr interpretar corretamente)
Lógica em cascata, primeira condição que bater certo vence:
1. Se houver erro → "error"
2. Senão, se já completo → "pausedUP"  (Sonarr interpreta isto como "pronto a importar")
3. Senão, se downloading no provider mas sem seeders → "stalledDL"
4. Caso contrário → "downloading"

NOTA: não precisamos de replicar toda a riqueza de estados do qBittorrent real —
Sonarr/Radarr só distinguem, na prática: downloading / pronto (pausedUP) / error.

## CONSTRUÇÃO DE PATHS (importante para o Sonarr encontrar os ficheiros)
| Cenário                          | save_path        | content_path                  |
|-----------------------------------|-------------------|--------------------------------|
| Sem categoria, multi-ficheiro     | /downloads        | /downloads/NomeTorrent/        |
| Categoria "tv-sonarr"             | /downloads/tv-sonarr | /downloads/tv-sonarr/NomeTorrent/ |
| Categoria "radarr", ficheiro único| /downloads/radarr | /downloads/radarr/NomeTorrent/ |

## O QUE NÃO PRECISAMOS DE IMPLEMENTAR (fora do escopo do MVP)
- Watch folders
- Múltiplos providers em simultâneo
- Upload/seed ratio (upspeed é só espelho de dlspeed, fake)
- Symlink downloader / rclone mount
- Unpacking de RAR (podemos assumir que os debrids já entregam ficheiros prontos)
