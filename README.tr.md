# OpenCode Session Manager (OSM)

n8n (veya herhangi bir HTTP istemcisi) ile **OpenCode** arasında duran küçük bir Windows / Linux işçisi.

n8n bir iş gönderir. OSM `repo_url`'i olduğu gibi klonlar, o işe özel bir `opencode serve` açar, oturumu bitirene kadar sürer, sonucu **metin** olarak geri verir. Ürün bir git push, MR veya dal değildir — son asistan mesajı (veya okunabilir bir hata)dır.

Tasarım: [PLAN.md](PLAN.md). Bağlayıcı kurallar: [AGENTS.md](AGENTS.md). İngilizce işletim kılavuzu: [README.md](README.md).

Bu belge projenin **nasıl çalıştığını** anlatır: akış, veri saklama, job modeli, her klasör / dosyanın görevi.

---

## 1. Bu ürün nedir, ne değildir

### Nedir

- n8n ↔ OpenCode **oturum / iş orkestratörü**
- Eşzamanlılık, kuyruk, clone, serve, session resume, retry, process kill, disk temizliği, per-job log
- Salt okunur bir **Jobs** panosu (`/jobs`)

### Ne değildir

- Yaver / `virtual_developer` değildir
- Jira poller yok, GitLab MR yok, Codex yok
- Dashboard yazmaz (iptal, sil, ayar, schedule yok)
- Git push / dal oluşturma / submodule / LFS indirme yok
- Paylaşılan tek bir `opencode serve` yok

Kasette "bug gibi duran" ama kasıtlı kararlar:

1. İşin ürünü **metindir**.
2. İş bitince clone **her zaman silinir** (başarı veya hata).
3. Chat ile disk arasındaki sapma beklenir: sonraki iş aynı yola temiz clone alır; eski oturum geçmişi silinmiş dosyalardan bahsedebilir.
4. Her işe **ayrı** `opencode serve`, benzersiz localhost port.
5. `callback_url` istek gövdesindedir; `Host` / `Origin`'den tahmin edilmez.
6. Dashboard yalnızca GET.
7. Hang saati "bu turda henüz cevap vermeye başlamadı"dır. İlk asistan mesajı gelince hang durur; donmuş üretim `timeout_in_seconds`'tır.

---

## 2. Büyük resim

```text
n8n / tester / curl
        │
        │  POST /jobs          (anında 202 / 400 / 409 / 503)
        │  GET  /jobs/{id}     (poller)
        │  DELETE /sessions    (admin, senkron)
        ▼
┌───────────────────────────────────────┐
│  OSM  (FastAPI, listen_port, örn. 4096)│
│                                       │
│  Manager  →  kuyruk / slot            │
│     │                                 │
│     ▼                                 │
│  Worker thread (job başına bir tane)  │
│     1. leftover clone'u sil           │
│     2. ls-remote + git clone          │
│     3. opencode serve (127.0.0.1:ephemeral)
│     4. session oluştur / resume       │
│     5. prompt + poll döngüsü          │
│     6. finish → callback (opsiyonel)  │
│     7. serve'i öldür, clone'u sil     │
└───────────────────────────────────────┘
        │
        │  tek terminal POST  (callback_url varsa)
        ▼
n8n Wait webhook  /  poller GET 200
```

İki bekleme modeli, **aynı OSM süreci**:

| Dosya | Nasıl bekler |
|---|---|
| [n8n-callback.json](n8n-callback.json) | `callback_url` = `$execution.resumeUrl`. OSM iş bitince **bir** POST atar. |
| [n8n-poller.json](n8n-poller.json) | `callback_url` yok. n8n `GET /jobs/{job_id}` ile poll eder. |

---

## 3. Veriler nerede tutulur

Tek kök: `data_dir` (`settings.yaml`).

| Platform | Varsayılan |
|---|---|
| Windows | `C:\osm` |
| Linux | `/var/lib/osm` |

Açılışta `Settings.ensure_dirs()` şu ağacı oluşturur:

```text
{data_dir}/
├── .temp/                          # clone'lar (iş bitince silinir)
│   └── {jira_id}/                  # örn. PROJ-123
├── .serve/                         # OpenCode serve stdout/stderr
│   └── {job_id}.log
├── logs/
│   ├── app.log                     # süreç geneli
│   ├── crash.log                   # yakalanmamış hata / sinyal
│   └── {jira_id}_{job_id}_{UTC}.log
├── jobs/
│   └── {job_id}.json               # kalıcı geçmiş (clone silinse de kalır)
└── queue.json                      # FIFO kuyruk (çalışan süreç için)
```

Yerel override: `settings.local.yaml` (gitignore). `OSM_SETTINGS` env ile başka YAML verilebilir.

### 3.1 Job geçmişi — `jobs/{job_id}.json`

Her kabul edilen iş **bir JSON dosyası**. Veritabanı yok. `JobStore` (`src/opencode_manager/dashboard/store.py`):

- Dosya adı: `job_<16 hex>.json`
- Yazma: atomik (`atomic.write_text_atomic` — tmp + `os.replace`, Windows kilitlerinde retry)
- Okuma: `json.loads` + `JobRecord.model_validate` (`model_validate_json` kullanılmaz)
- 50 MB üstü satır atlanır
- `list_all()` 3 saniyelik bellek önbelleği kullanır; `save()` önbelleği düşürür
- Dashboard `/ws` her tikte `list_all()` çağırmaz; `Manager.live_counts()` yeter

Clone silinse, süreç yeniden başlasa bile satır **kalır**. Dashboard geçmişi buradan okur.

Örnek alanlar (`JobRecord`):

| Alan | Anlamı |
|---|---|
| `job_id` | `job_` + 16 hex (UUID'den) |
| `jira_id` | Dedup anahtarı **ve** clone klasör adı |
| `status` | `queued` / `running` / `success` / `error` / `timeout` / `not_found` |
| `live` | Çalışıyor veya kuyrukta mı |
| `prompt` | Gelen ORIGINAL metin (dashboard `public_dict`'te gizlenir; ayrı `/prompts` var) |
| `callback_url` | İstekteki URL (`public_dict`'te gizlenir) |
| `session_id` | OpenCode `ses_*` |
| `clone_path` | `{data_dir}/.temp/{jira_id}` |
| `serve_pid` / `serve_port` / `serve_base_url` | Bu işin serve'i |
| `extra_pids` | Canlı git çocukları |
| `original_posted` | ORIGINAL prompt bir kez POSTlandı mı |
| `session_bound` | Bu işte geçerli bir `ses_*` bağlandı mı |
| `attempts[]` | Deneme satırları (hang, serve-dead, incomplete, timeout…) |
| `prompts[]` | OSM'nin POSTladığı metinler + zaman |
| `chat_snapshot[]` | Serve öldükten sonra dashboard sohbeti |
| `text` | Terminal ürün (son asistan mesajı veya hata) |
| `callback_status_code` | 200 / 404 / 500 / 504 |
| `log_file` | Per-job log dosya adı |

Canlı durumlar: `queued`, `running`. Aynı `jira_id` için ikincisi **409**.

### 3.2 Kuyruk — `queue.json`

Kapasite doluysa (`max_concurrent_jobs`) **başka** ticket'lar FIFO'ya yazılır. `callback_url` dahil tüm istek gövdesi persist edilir ki **aynı çalışan süreç** slot boşalınca dequeue edebilsin.

**Süreç restart kuyruğu otomatik çalıştırmaz.** Boot leftover'ları ERROR işaretler, kuyruğu boşaltır, callback göndermez.

`JobQueue` API: `enqueue`, `dequeue` (baştan pop), `peek_all`, `clear`, `public_items` (dashboard).

### 3.3 Clone — `.temp/{jira_id}`

- Klasör adı yalnızca ticket (`jira_id`). Repo / dal yolda yok.
- Windows-güvenli: `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$`. `.`, `..`, slash → inbound **400**.
- Hedef, `.temp` kökünün **sıkı çocuğu** olmalı (kökün kendisi değil).
- Yeni iş: yol varsa önce hard-delete, sonra clone. Silinemezse job **500**, OpenCode yok.
- Mid-job retry clone'u **silmez**.
- İş bitince (git hatası dahil) yine silinir.

### 3.4 Loglar

| Dosya | Ne |
|---|---|
| `{data_dir}/logs/app.log` | Tüm süreç |
| `{data_dir}/logs/crash.log` | Yakalanmamış exception, sinyal, faulthandler |
| `{data_dir}/logs/{jira_id}_{job_id}_{YYYYMMDD}_{HHMMSS}.log` | İş başına bir dosya (kabul anı, UTC) |
| `{data_dir}/.serve/{job_id}.log` | O işin `opencode serve` stdout/stderr |
| `{proje}/logs/wrapper-exit.log` | Windows güvenlik süreci öldürürse start script exit kodu yazar (Python yazamaz) |

Her satır `job_id` + `jira_id` taşır (`log_context` contextvars). URL'deki `user:pass@`, Azure `user@`, `:pass@` redakte edilir. `PAT` anahtarı loglanmaz.

### 3.5 OpenCode'un kendi verisi

OSM bir SQLite tutmaz. OpenCode oturumları kullanıcının global `opencode.db` dosyasındadır (`directory` anahtarlı). Aynı clone yolu + geçerli `ses_*` → resume çalışır.

Dashboard, serve öldükten sonra **bu işin** `chat_snapshot`'ını gösterir. Global DB'den aynı `ses_*` ile sonraki işlerin turlarını eklemez (aynı ticket sonra aynı `ses_*` / yolu yeniden kullanır). Eksik tool `output` doldurulabilir; sonraki turlar eklenmez.

---

## 4. Bir işin yaşam döngüsü

### 4.1 Kabul (`POST /jobs`)

`Manager.submit` soketi **tutmaz**. Clone / OpenCode arka planda.

```text
boot bitmedi veya shutdown  →  503, callback yok
alan hatası / SSH / kötü model / kötü agent  →  400, callback yok
callback host allow-list dışı  →  400
aynı jira_id canlı (queued/running)  →  409
aynı jira_id için session delete uçuşta  →  409
slot boş  →  202 "in progress", thread başlar, 1 terminal callback (URL varsa)
slot dolu, başka ticket  →  202 "queued", queue.json, sonra dequeue + 1 callback
```

Gerekli gövde:

| Alan | Not |
|---|---|
| `repo_url` | `http` / `https` / `file`. `git@` / `ssh://` yasak. |
| `source_branch` | Uzakta var olmalı. `-1` / boş → 400. Uzakta yok → 202 sonra callback **404**. |
| `prompt` | ORIGINAL; job boyunca **bir kez** POST. |
| `model` | `provider/id` (örn. `opencode/mimo-v2.5-free`) |
| `agent_mode` | Yalnız `planner` veya `orchestrator`. n8n `working_mode`'u kendisi map'ler. |
| `timeout_in_seconds` | **Bir** OpenCode denemesi (serve boot + session loop). Clone / cleanup dışı. |
| `retry_count` | Deneme sayısı (ilki dahil). Min 1. `3` ve `1800` → en fazla 5400s OpenCode. |
| `jira_id` | Dedup + klasör. |
| `callback_url` | Opsiyonel. Boş = poll. Varsa mutlak `http(s)`. |
| `session_id` | Opsiyonel `ses_*`. Boş / `-1` / Codex UUID = yeni oturum aç. |

Aynı zarf hem ack hem callback / poll için:

```json
{
  "text": "…",
  "session_id": "ses_… veya boş",
  "status_code": 202,
  "jira_id": "PROJ-123",
  "job_id": "job_…"
}
```

`queued` / `in_progress` **callback'i asla yoktur**. Poller `GET /jobs/{id}` canlıysa HTTP **202** + `live: true`; terminalde HTTP **200** ve zarf `status_code` 200 / 404 / 500 / 504. Bilinmeyen id → HTTP **404**.

### 4.2 Worker pipeline

`worker.OpenCodeRunner.run`:

1. `clone_path_for(work_dir, jira_id)` — yol kaydı.
2. Yol varsa hard-delete. Silinemezse **500**.
3. `ls-remote` ile `refs/heads/{branch}` **tam eşleşme**. Yoksa **404**.
4. `git clone <url> dest` — `--branch` yok, checkout yok, submodule yok, LFS skip (`GIT_LFS_SKIP_SMUDGE=1`), `GIT_TERMINAL_PROMPT=0`.
5. Origin userinfo scrub (saklanan `remote.origin.url`; `git remote get-url` yok).
6. `run_opencode_job` (dış retry + iç poll).
7. `finally`: bu işin process ağacını öldür, clone'u sil.

Git auth:

- Windows: önce GCM (`manager`, `GCM_INTERACTIVE=auto`). Auth hatası → bir kez `Get-Credential` diyaloğu, Basic retry (argv'de / logda yok). İptal / boş → **500**.
- Linux: `credential.helper` boş, diyalog yok.
- Gelen `PAT` alanı yok sayılır.

### 4.3 OpenCode'u nasıl yönetiriz

OSM, OpenCode'u **CLI one-shot** (`opencode --auto`) olarak çalıştırmaz. Her iş için ayrı bir HTTP sunucusu açar ve o sunucuyu bir **durum makinesi** ile sürer. Kod: `opencode/serve.py` (süreç), `opencode/session.py` (HTTP istemci + idle kararı), `opencode/retry.py` (dış deneme + iç poll), `opencode/prompts.py` (dört sabit string).

İki iç içe döngü vardır. Dış döngü **kaç kez yeni serve / yeni tur** deneneceğini sayar. İç döngü **o tur bitene kadar** OpenCode'u 1 saniyede bir yoklar. `/message` soketini deneme bütçesi boyunca bloklamayız.

```text
run_opencode_job                          ← dış döngü (retry_count kez)
  │
  ├─ serve yoksa: opencode serve başlat
  ├─ health + dizin instance + model envanteri
  ├─ session resume veya create
  ├─ bir user prompt POST
  │
  └─ _inner_loop                          ← iç döngü (~1s poll)
        GET /global/health
        GET /session/status
        GET /session/:id          (compact flag)
        GET /session/:id/message
             │
             ├─ compacting          → bekle, hang yok
             ├─ busy, asistan geldi → bekle, hang yok (saat: timeout)
             ├─ busy, asistan yok   → hang saati
             ├─ POST sonrası idle   → bir kaç tick bekle (busy henüz flip olmamış olabilir)
             └─ gerçek idle         → assess_idle
                  success / question / compact_leftover / incomplete
```

#### Neden `opencode serve`, neden iş başına bir tane

| Seçenek | Neden kullanmıyoruz / kullanıyoruz |
|---|---|
| `opencode --auto` | Tek seferlik CLI. Sabit session HTTP API yok. Durum makinesi kurulamaz. |
| Tek paylaşılan serve | Clone silmek için serve'i öldürmek gerekir; paylaşıksa diğer işler de ölür. Windows kilit / AV yüzünden kill şart. |
| **İş başına serve** | Bu iş bitince yalnızca **bu** pid ağacı ölür. Diğer işler etkilenmez. |

Komut her zaman:

```text
opencode serve --hostname 127.0.0.1 --port <boş port> --print-logs --log-level INFO
```

- Port `4096` **değildir** (o OSM'nin kendi portu). `127.0.0.1:0` bağlanır, OS boş port verir, soket kapanır, serve o numarayla açılır.
- `cwd` = clone. Permission auto-approve **yok**.
- `OPENCODE_SERVER_PASSWORD` boşaltılır.
- `start_new_session=True` — Linux'ta process group, kill izole.
- stdout/stderr `{data_dir}/.serve/{job_id}.log` (append; dış retry önceki çıktıyı silmez).
- Popen **hemen** sonra `{serve_pid, serve_port, serve_base_url}` job JSON'a yazılır. Health beklemeden önce kaydedilir ki crash olsa boot leftover'ı öldürebilsin.
- n8n bu portları görmez. Tüm istekler `http://127.0.0.1:<port>` + header `x-opencode-directory: <clone>`.

İki sağlık katmanı vardır; karıştırılmaz:

| Kontrol | Ne söyler | Ne söylemez |
|---|---|---|
| `GET /global/health` → 200 | Süreç ayakta | Clone için instance hazır değil |
| `GET /session` (directory header ile) | Dizin instance cevap veriyor | — |

İlk `x-opencode-directory` isteği OpenCode'un clone'u bootstrap etmesini bekletebilir (büyük ağaç). Bu bekleme **deneme saati** içindedir. 30 saniyede `POST /session` patlatılmaz.

Sonra `GET /config/providers`, olmazsa `/provider`. İstenen `model` (`provider/id`) envanterde yoksa veya envanter okunup boşsa iş **hemen 500**. User message yok, `retry_count` harcanmaz. Sonradan gelen `ProviderModelNotFoundError` / "model not found" de aynı 500 — hang veya 504 değil.

#### Session id — iki an

OpenCode oturumları global `opencode.db` içinde `directory` (clone yolu) ile anahtarlanır. Aynı yol + geçerli `ses_*` → resume. OSM iki durumu ayırır:

| An | Kullanılamayan / reddedilen `ses_*` |
|---|---|
| **Henüz canlı id yok** (inbound boş / `-1` / `ses_*` değil, veya ilk serve create'ten önce öldü) | Yeni session aç. INFO log. İşi fail etme. Geri dönen id gerçekten kullanılan id'dir. |
| **Mid-job hang retry** (bu işte zaten `session_bound`, clone diskte) | **Bu deneme fail.** Boş session açıp devam etme. `retry_count`'a sayılır. |

`resume_or_create`: inbound `ses_*` ise `GET /session/:id`. 200 → resume. Değilse create. Ama `session_bound` iken create dönerse `AttemptFailed("create-fail")` — "will not open a blank session".

#### OSM'nin konuştuğu OpenCode HTTP

Tüm çağrılar `OpenCodeClient` üzerinden, yalnızca **bu işin** `base_url`'ine.

| OSM ne yapar | OpenCode yolu |
|---|---|
| Süreç ayakta mı | `GET /global/health` |
| Instance hazır mı | `GET /session` |
| Model var mı | `GET /config/providers`, sonra `/provider` |
| Session al / aç | `GET /session/:id`, `POST /session` |
| Durum (idle / busy / retry) | `GET /session/status` — tipi yalnızca bunlar; **compacting diye bir status yok** |
| Compact flag | `GET /session/:id` → `time.compacting` |
| Mesaj listesi | `GET /session/:id/message?limit=400` |
| User prompt | önce `POST /session/:id/prompt_async`; olmazsa `POST /session/:id/message` (arka plan thread, 45s içinde busy veya history'de görünmeli) |
| Kes | `POST /session/:id/abort` |
| Admin unut | `DELETE /session/:id` (`DELETE /sessions` bunu kullanır) |

Her user POST gövdesi:

```json
{
  "agent": "planner | orchestrator",
  "parts": [{ "type": "text", "text": "…" }],
  "model": { "providerID": "…", "modelID": "…" }
}
```

`busy` / compacting iken POST **yok**. Session zaten çalışıyorsa OSM "Continue" yarışı yapmaz.

#### Beş prompt — başka string yok

ORIGINAL bir kez gider; anahtar **deneme numarası değil**, `original_posted` (OpenCode o POST'u kabul etti mi).

| `prompt_id` | Ne zaman | Kaynak |
|---|---|---|
| `ORIGINAL` | Bu işte henüz POSTlanmadı | İstekteki `prompt` |
| `UNATTENDED_NUDGE` | Idle + model soru sordu | `prompts.py`, iş başına **en fazla bir** |
| `COMPACT_LOOP_NUDGE` | Bu turda ~8 yeni compact marker, veya compact leftover | `prompts.py` |
| `HANG_RESUME` | ORIGINAL gittikten sonra dış retry (hang / serve-dead / timeout / HTTP) | `prompts.py` |
| `INCOMPLETE_RESUME` | Idle, finish temiz `stop` değil, **aynı serve** | `prompts.py` |

OSM asla şunu göndermez: `Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.` Bu satır OpenCode'un kendisinden gelir: başarılı **auto** compact sonrası sentetik user part (`synthetic: true`, `metadata.compaction_continue: true`). `SessionPrompt.run` son asistan `finish != tool-calls` ve `lastUser.id < lastAssistant.id` olunca çıkar. Compact özeti `finish=stop` bir asistandır; yeni user yoksa serve idle olur, OSM bunu **success** sanıp işi bitirirdi. Bu yüzden `experimental.compaction.autocontinue` kapatılmaz, sentetik tur silinmez. Dashboard onu normal user balonu gösterebilir; OSM prompt listesine yazmaz.

#### Dış döngü (`run_opencode_job`)

`retry_count` = deneme sayısı (ilki dahil). Her deneme kendi `timeout_in_seconds` saatini **sıfırdan** kurar. `3` × `1800` → en fazla 5400s OpenCode (clone / cleanup / callback dışı). Denemeler arası üstel backoff: `retry_backoff_seconds` … `retry_backoff_cap_seconds`.

Her denemenin başı:

1. Serve yoksa başlat + health + `wait_directory` + model envanteri.
2. Session bağla (`session_bound = true`).
3. Prompt seç: `original_posted` değilse ORIGINAL; değilse varsayılan `HANG_RESUME`; bir önceki deneme `incomplete` ise `INCOMPLETE_RESUME`.
4. Tur öncesi mesaj listesini al → **baseline**: son asistan id, mesaj sayısı, compact marker sayısı. Resume edilmiş `ses_*` geçmişi compact-loop sayacına **girmez**.
5. `_post_user` + `_inner_loop`.

İç döngüden çıkan sonuca göre:

| Sonuç | Serve | Sonraki deneme prompt'u | `retry_count` |
|---|---|---|---|
| `success` | iş bitince ölür | — | — (200) |
| `asking` (nudge sonrası hâlâ soru) | iş bitince ölür | — | iş 500, retry yok |
| `compact_leftover` (nudge sonrası) | iş bitince ölür | — | iş 500, retry yok |
| `timeout` | **öldür**, yenisini aç | ORIGINAL henüz gitmediyse ORIGINAL, yoksa `HANG_RESUME` | evet |
| `hang` / `serve-dead` | **öldür**, yenisini aç | aynı | evet |
| `incomplete` | **aynı serve kalır** | `INCOMPLETE_RESUME` | evet |

Model yok / `JobFailed` → dış döngü kırılır, iş 500. Denemeler tükenince: son kind `timeout` ise **504**, aksi **500**. `finally` her durumda `close_serve` (abort + stop_serve + pid temizle). Clone silmek dış döngünün işi değildir; onu worker `finally` yapar.

#### İç döngü (`_inner_loop`) — turu sürmek

`/message`'i açık tutmayız. Yaklaşık 1s'de bir (POST sonrası ilk idle için 0.4s) şunları okuruz:

1. Shutdown? → iş 500.
2. Deneme saati (`deadline`) doldu mu? → abort, `timeout`.
3. `GET /global/health` öldü mü? → `serve-dead`.
4. Status + session payload + mesaj listesi. `chat_snapshot` her tick güncellenir (dashboard canlı sohbet).
5. Bu turda yeni asistan var mı? (`id ≠ baseline`). Varsa `answered_this_turn = true` ve `job.text` = son asistan metni. **Kasasındaki anlam:** hang = "hiç cevap vermeye başlamadı". Mid-generation donması hang değil, deneme saatidir.
6. İlerleme: yeni mesaj, yeni compact marker, `compacting`, veya yeni asistan → `last_progress` sıfırlanır, hang saati durur.

Faz (log `session phase=`):

| Faz | Koşul | Ne yaparız |
|---|---|---|
| `compacting` | status'ta compact **veya** `time.compacting` | Dakikalar beklenir. Hang **yok**. POST **yok**. |
| `busy` + bu turda asistan var | model üretiyor / tool çağırıyor | Bekle. Hang yok. Saat: `timeout_in_seconds`. |
| `busy` + henüz asistan yok | POST gittikten sonra cevap yok | `hang_timeout_seconds` (varsayılan 180) işler. Doluysa abort → `hang`. |
| `awaiting` | POST yeni gitti, status henüz idle görünebilir | Birkaç tick bekle; erken `assess_idle` yapma. |
| `idle` | gerçekten durdu | `assess_idle` |

`GET /session/status` yalnızca `idle` / `retry` / `busy` döner. Compact'ı status tipi sanmayız; `GET /session/:id` içindeki `time.compacting` (ve status string'inde "compact") bakılır.

**`assess_idle`** son mesaja bakar, dört hüküm:

| Hüküm | Ne zaman | OSM ne yapar |
|---|---|---|
| `success` | son finish tam olarak `stop` (temiz bitiş). Yalnız leftover OpenCode todo başarıyı bozmaz | iş 200, `text` = son asistan |
| `question` | asistan metni soru gibi (`?` + "shall i" / "do you want" … veya kısa `?`) | bir kez `UNATTENDED_NUDGE`; yine sorarsa iş 500 |
| `compact_leftover` | son mesaj compact kokuyor ve asistan değil | bir kez `COMPACT_LOOP_NUDGE`; yine varsa iş 500 |
| `incomplete` | `tool-calls`, `length` / max token, `null`, son mesaj user, boş liste… | dış döngüye `incomplete` — **aynı serve**, `INCOMPLETE_RESUME` |

Compact-loop: bu **beklemede** `compact_marker_count - baseline ≥ 8` ve hüküm henüz success değilse abort et, idle olmasını bekle (en fazla 60s), `COMPACT_LOOP_NUDGE` at, iç döngüde kal. Resume geçmişindeki eski marker'lar baseline'dadır, sayılmaz.

#### Hang vs timeout vs incomplete — karıştırılan üç saat

```text
                    bu turda asistan geldi mi?
                         │
              hayır ─────┴───── evet
                │                  │
         hang saati           hang yok
         (180s, busy          üretim sonsuza
          + compact değil     kadar sürebilir;
          + yeni marker yok)  sınır: timeout_in_seconds
                │                  │
              hang              timeout
           serve ölür         serve ölür
           HANG_RESUME        HANG_RESUME
```

Incomplete ayrı: session **idle**, iş bitmemiş. Serve ölmez.

#### Bilerek yapmadıklarımız

- `opencode --auto` yok.
- Permission auto-approve yok.
- Paylaşılan serve yok, sabit port `4096` job serve'e verilmez.
- Compact sırasında user POST yok.
- Mid-job'da boş session açılmaz.
- ORIGINAL ikinci kez gitmez.
- Beşincisi uydurulmuş "Continue" yok.
- Model yokken retry yok.
- Başka işin serve'i / OSM süreci öldürülmez.
- Hang retry clone'u silmez; yalnızca **bu** serve ölür.

### 4.4 Job sonu

Sıra her zaman:

1. Session abort (best effort)
2. Bu işin ağacı: git, **bu** serve, tool çocukları (`taskkill /F /T` / `SIGKILL` process group)
3. Clone cwd/argv leftover (Linux `/proc`). Windows'ta tüm süreç taraması / PowerShell `Win32_Process` **yok**. Kalan kilitler Restart Manager ile, **ayrı child process** içinde (AV OSM'yi öldürmesin). En fazla iki child. RM sonrası `path_has_holders` yok.
4. Holder kalmadıysa stale `.git/*.lock`
5. Sıralı hard-delete (Windows `rd /s /q \\?\…`, Linux chmod + rmtree)

**OSM asla öldürülmez.** `kill_pid` / `may_kill` bu süreci, atalarını (`cmd` / `start-backend.bat`), PID ≤ 4'ü reddeder. Başka işin serve'ine dokunulmaz.

Hang / timeout / serve-dead dış retry: 2. adımdan sonra dur, yeni serve — clone silinmez. Incomplete retry bu kill yoluna hiç girmez.

Sonra `finish_job`: `live=false`, status map (200→success, 404→not_found, 504→timeout, diğer→error), JSON kaydet, `callback_url` varsa **bir** terminal POST.

Callback HTTP (n8n'in o POST'a cevabı, zarf `status_code` değil):

- `2xx` → teslim, dur
- `404` / `408` / `429` / `5xx` / transport → `callback_retry_count` kadar aynı zarf
- Diğer `4xx` → kalıcı, logla, dur
- OpenCode yeniden çalıştırılmaz. n8n Wait `404` = webhook henüz silahlanmamış.

Boot leftover ERROR satırlarına callback **yok**.

### 4.5 Boot ve shutdown

Boot (iş kabulünden önce):

- Kayıtlı leftover `serve_pid` / `extra_pids` öldür
- Linux'ta `work_dir` orphan reap; Windows'ta Win32 snapshot yok
- leftover queued/running satırları ERROR (`process restarted; leftover job was not resumed`), callback yok
- `queue.json` temizle
- Ancak o zaman `ready=true`

Shutdown:

- `POST /jobs` / `DELETE /sessions` kes
- Her işin ağacını zorla öldür
- running **ve** queued → ERROR + callback **500**
- clone sil
- worker thread'leri bekle
- ancak o zaman süreç çıksın

Boot leftover veya shutdown ERROR'dan sonra aynı `jira_id` için yeni `POST /jobs` **yeni iştir**. Worker leftover yolu önce siler, sonra clone'lar.

### 4.6 `DELETE /sessions`

Senkron admin; kuyruk / geçmiş satırı yok. Gövde `{ jira_id, session_id }`.

- Boş / `-1` / `ses_*` değil → **400**
- O ticket canlı queued/running → **409** (`job_id` dolu)
- Aynı ticket için delete uçuşta → **409**
- OpenCode 2xx veya 404 → **200** (idempotent)
- Serve boot / diğer OpenCode hatası → **500**
- Boot / shutdown → **503**

---

## 5. HTTP yüzeyi

Yazma yalnızca `POST /jobs` ve `DELETE /sessions`. Dashboard `/api/*` GET-only; yazma **405**.

| Metod | Yol | Kim |
|---|---|---|
| POST | `/jobs` | n8n / tester |
| GET | `/jobs/{job_id}` | Poller (callback ile aynı zarf + `live`/`status`) |
| DELETE | `/sessions` | Admin |
| WS | `/ws` | Dashboard canlı sayaç (`running`, `queue_queued`) |
| GET | `/api/meta` | Sürüm |
| GET | `/api/jobs?filter=&jira_id=&page=` | Liste (filtre sunucuda) |
| GET | `/api/jobs/{id}` | Detay + sistem log özeti |
| GET | `/api/jobs/{id}/prompts` | OSM'nin POSTladığı prompt'lar |
| GET | `/api/jobs/{id}/chat` | Canlı serve veya bu işin snapshot'ı |
| GET | `/api/jobs/{id}/logs?limit=` | Per-job manager log (`0` = tamamı) |
| GET | `/api/jobs/{id}/serve-log` | OpenCode serve log (redakte) |
| GET | `/api/queue?jira_id=` | Kuyruk |
| GET | `/jobs`, `/jobs/:id` | SPA |

Canlı chat: yalnızca `session_id` `ses_*` ise OpenCode'a gidilir. `-1` / boş ile `/session/-1/message` çağrılmaz.

---

## 6. Dashboard (GET only)

React + Vite + Tailwind + Geist. `virtual_developer` jobs sekmesinin görünümü; yazma yok.

- `/` → `/jobs`
- `/jobs` — All / In flight / Error / Completed / Queue, `jira_id` arama, sayfalama
- `/jobs/:jobId` — meta, denemeler, prompt'lar, sohbet, OSM log, serve log
- **Report issue**: yerel zip (not + job JSON + prompt + chat + OSM log + serve log). Not saklanmaz.
- Bilinmeyen id önceki işi ekranda bırakmaz

İki sunum:

- Manager `4096` üzerinde API + aynı SPA
- `start-frontend` Python proxy `:5173` (`dashboard.frontend_proxy`) — hedefte Vite/npm yok

---

## 7. Ayarlar (`settings.yaml`)

| Anahtar | Ne işe yarar |
|---|---|
| `listen_host` / `listen_port` | OSM HTTP (varsayılan `0.0.0.0:4096`) |
| `max_concurrent_jobs` | Aynı anda kaç serve (RAM bütçesi; bir serve ~300–500 MB) |
| `callback_timeout_seconds` | Bir callback POST bekleme |
| `callback_retry_count` | 5xx / ağ / 404-408-429 |
| `callback_allowed_hosts` | `[]` / `["*"]` / `["all"]` = herkes. Aksi halde SSRF listesi (`*.example.com` olur) |
| `data_dir` | Tek yazma kökü |
| `log_level` | DEBUG … CRITICAL |
| `opencode_bin` | PATH adı veya mutlak yol |
| `hang_timeout_seconds` | İlk asistan yokken hang (varsayılan 180) |
| `git_clone_timeout_seconds` | clone / ls-remote (varsayılan 1800) |
| `retry_backoff_seconds` / `_cap` | Dış retry üstel bekleme |

---

## 8. Kaynak ağacı — her dosya ne yapar

### Kök

| Dosya | Görevi |
|---|---|
| `AGENTS.md` | Uygulayıcı için bağlayıcı kurallar. PLAN ile çelişirse PLAN düzeltilir. |
| `PLAN.md` | Uzun tasarım (neden per-job serve, prompt tablosu, kill sırası). |
| `README.md` | İngilizce işletim / n8n / curl sözleşmesi. |
| `README.tr.md` | Bu belge. |
| `settings.yaml` | Operatör ayarları. |
| `settings.local.yaml` | Makine override (gitignore). |
| `opencode.json` | Yerel OpenCode yapılandırması (geliştirme). |
| `VERSION` | Dağıtım sürümü. |
| `pyproject.toml` | Paket, bağımlılıklar, `opencode-manager` CLI giriş noktası. |
| `pytest.ini` | Test yolları / marker. |
| `n8n-callback.json` | n8n alt-akış: Wait webhook + `callback_url`. |
| `n8n-poller.json` | n8n alt-akış: `GET /jobs/{id}` döngüsü. |
| `n8ninitial.json` | Eski / başlangıç n8n parçası; kamu sözleşme değil. |

### `src/opencode_manager/` — süreç

| Dosya | Görevi |
|---|---|
| `__init__.py` | `__version__` |
| `app.py` | FastAPI fabrikası, lifespan (`boot` / `shutdown`), `/ws`, uvicorn `main` |
| `api.py` | Tüm HTTP rotaları + SPA mount + dashboard yazmalarını 405 |
| `manager.py` | Kabul, 409/400/503, kuyruk, thread, boot leftover, shutdown, `DELETE /sessions`, `live_counts` |
| `worker.py` | Bir iş: clone → OpenCode → `finish_job` (callback) → clone sil |
| `models.py` | `JobRequest`, `JobRecord`, `Envelope`, `AttemptRow`, `PromptRow`, doğrulama, poll zarfı, host allow-list |
| `settings.py` | YAML yükle, `data_dir` türevi yollar, `ensure_dirs` |
| `queue.py` | `queue.json` FIFO |
| `callback.py` | Terminal POST, retry sınıflandırması |
| `atomic.py` | Windows okuyucu kilitlerine dayanıklı atomik yazım |
| `log.py` | App + per-job dosya log, redact, komut/HTTP yardımcıları |
| `log_context.py` | contextvars: `job_id`, `jira_id`, `log_file` |
| `crash.py` | `crash.log`, faulthandler, temiz çıkış işareti |

### `src/opencode_manager/git/`

| Dosya | Görevi |
|---|---|
| `clone.py` | `clone_path_for`, `ls-remote` tam ref, `git clone`, origin scrub, PID takibi |
| `auth.py` | İzole git env, GCM, Windows `Get-Credential`, Basic retry, cred bellek |
| `detect.py` | Host sınıflandırma (gitlab / azure / tfs) — log için |

### `src/opencode_manager/opencode/`

| Dosya | Görevi |
|---|---|
| `serve.py` | Port seç, `opencode serve` başlat/durdur, health bekle, serve log oku |
| `session.py` | HTTP istemci: session create/resume, message POST, poll, assess_idle, model envanteri, chat snapshot |
| `retry.py` | Dış retry + iç poll, hang/compact/incomplete, prompt seçimi |
| `prompts.py` | Dört sabit string (`UNATTENDED_NUDGE`, `COMPACT_LOOP_NUDGE`, `HANG_RESUME`, `INCOMPLETE_RESUME`) |

### `src/opencode_manager/cleanup/`

| Dosya | Görevi |
|---|---|
| `end.py` | Job-end sırası: ağaç öldür → leftover → RM holders → lock düşür → `hard_delete` |
| `kill.py` | `kill_pid` / `may_kill` (OSM'yi asla öldürme), Linux reap, Windows RM child |
| `rmtree.py` | Windows `rd /s /q \\?\` + reserved names; Linux chmod + rmtree |

### `src/opencode_manager/dashboard/`

| Dosya | Görevi |
|---|---|
| `store.py` | `jobs/*.json` JobStore |
| `chat.py` | Canlı sohbet veya snapshot; gerekirse `opencode.db`'den **eksik tool output** doldur (sonraki tur ekleme) |
| `frontend_proxy.py` | `:5173` SPA + `/api` `/ws` reverse proxy (offline paket, Vite yok) |

### `web/` — SPA

| Dosya | Görevi |
|---|---|
| `src/main.tsx` | React giriş |
| `src/index.css` | Tailwind / tema |
| `src/app/App.tsx` | Router: `/` → `/jobs`, `/jobs/:jobId` |
| `src/app/Shell.tsx` | Kabuk layout |
| `src/app/LiveProvider.tsx` / `live.ts` | `/ws` canlı sayaç |
| `src/api/client.ts` | GET istemcisi |
| `src/api/types.ts` | TS tipleri |
| `src/pages/jobs/JobsPage.tsx` | Liste + filtre + kuyruk |
| `src/pages/jobs/JobDetailPage.tsx` | Detay sekmeleri + report zip |
| `src/pages/jobs/JobChatTab.tsx` | Sohbet UI |
| `src/pages/jobs/filters.ts` | Filtre anahtarları |
| `src/ui/*` | StatusBadge, Tabs, MarkdownBody, ReportIssueDialog, … |
| `src/util/jobReport.ts` / `zipStore.ts` / `download.ts` | Yerel issue zip |
| `src/util/chatParts.ts` / `chatMarkup.ts` / `time.ts` | Sohbet parçaları, markdown, zaman |
| `web/dist/` | Offline / manager'ın sunduğu build |

### `scripts/`

| Dosya | Görevi |
|---|---|
| `install.bat` / `install.sh` | Manager: bundled Python ile `.venv`, `vendor/python-wheels`, `web/dist` kontrol. Ağ yok. |
| `install-opencode.bat` / `.sh` + `install_opencode.py` | `<user>/.opencode` sil, `vendor/bin` kopyala |
| `start.bat` / `start.sh` | Backend + frontend |
| `start-backend.*` / `run-backend.bat` | uvicorn / manager |
| `start-frontend.*` / `serve_frontend.py` | Python SPA proxy |
| `osm-lib.sh` | Linux start/install ortak |
| `request_storm.py` | Yük / fırtına testi |

### `packaging/`

| Dosya | Görevi |
|---|---|
| `build_dist.py` | Offline zip: CPython + wheels + OpenCode CLI + `web/dist`. `agents/` zip'e girmez. |
| `versions.env` | Pin'lenen sürümler |

CI dört zip üretir: `*-windows-x64`, `*-linux-x64`, `*-darwin`, `*-windows-linux`.

### `tester/`

Sahte n8n Wait. OSM'ye `POST /jobs`, `:8090/callback` dinler, `replies.jsonl` yazar. Ürünün parçası değil.

### `agents/`

| Dosya | Görevi |
|---|---|
| `fixed-conditions.md` | Kapanmış kusur listesi. Bunları açık bug diye tekrar etme. Davranış değişirse `tests/test_fixed_conditions.py` aynı değişiklikte güncellenir. |

### `tests/`

İşaretler: varsayılan birim / sahte; `-m live` gerçek serve + gerçek clone.

Önemli gruplar:

| Dosya | Konu |
|---|---|
| `test_api.py` | Ack / 400 / 409 / 503 |
| `test_job_poll.py` | `GET /jobs/{id}` |
| `test_callback.py` | Callback retry |
| `test_session_delete.py` | `DELETE /sessions` |
| `test_models.py` | Doğrulama |
| `test_git*.py` | Clone, branch, origin scrub, PAT yok |
| `test_compact_hang.py` / `test_session_assess.py` / `test_finish_and_prompts.py` | Compact / hang / prompt |
| `test_cleanup_*.py` / `test_job_end_*.py` | Kill + delete, OSM hayatta |
| `test_shutdown_and_boot_reap.py` | Boot leftover, shutdown 500 |
| `test_dashboard_filters.py` / `test_job_store_save.py` / `test_job_chat_isolation_e2e.py` | Pano / store / chat izolasyonu |
| `test_fixed_conditions.py` | Kapanmış kusur regresyonu |
| `test_live_job_e2e.py` / `test_session_resume_same_path_live_e2e.py` | Canlı e2e |
| `test_offline_packaging.py` / `test_install_opencode.py` | Offline paket |
| `conftest.py` / `job_end_helpers.py` / `helpers/` | Ortak fixture |

---

## 9. Çalıştırma (kısa)

Hedefte ağ yok. Offline zip + Git yeterli. Python zip'in içinde.

```bash
# Windows
install.bat
install-opencode.bat
start.bat

# Linux
./install.sh
./install-opencode.sh
./start.sh
```

Kaynaktan:

```bash
python3.12 -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
python -m pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
opencode-manager
```

- API: http://127.0.0.1:4096
- Dashboard: http://127.0.0.1:4096/jobs
- Sağlık: `GET /api/meta`
- Sahte n8n: `python tester/tester.py` → form `:8090`, callback `http://127.0.0.1:8090/callback`

```bash
python -m pytest tests -m "not live"
python -m pytest tests -m live
```

curl örnekleri ve n8n ack tabloları için [README.md](README.md).

---

## 10. Bilerek kopyalanmayanlar (`virtual_developer`)

Jira poller, GitLab MR / `glab`, Codex, dashboard yazmaları, Poll / Scheduled / Sessions / Storage / Settings sekmeleri, PAT haritası, `feature/KEY` dalı üretmek, paylaşılan uzun ömürlü serve, "serve'i asla öldürme", kirli clone'u reuse için tutmak.

Kopyalananlar: git no-console-prompt env, process-tree kill, Windows hard delete, compact-bekle / tek nudge, dış retry şekli, per-job log context, jobs-tab görünümü (GET-only).
