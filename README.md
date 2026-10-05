# PTZ Strobo API

API HTTP JSON mandiri untuk mengendalikan kamera PTZ melalui NVR memakai DVRIP/Sofia (TCP port `34567`). Dibangun dengan FastAPI; Swagger UI dan OpenAPI dibuat otomatis. Paket ini tidak memerlukan dashboard atau `stream_api`.

## Persiapan

Butuh Python 3.10+ dan akses jaringan dari mesin API ke NVR. Masuk ke folder ini (`ptz_api` pada proyek lokal, atau folder hasil clone `ptz-strobo`), lalu jalankan:

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Isi `.env` lokal sesuai perangkat. Jangan mengunggah atau membagikan `.env` karena mengandung kredensial; file ini sudah diabaikan oleh Git. Format URL NVR tersedia di `.env.example` (jika kredensial mengandung karakter khusus, URL-encode bagian username/password).

| Variabel | Keterangan |
| --- | --- |
| `RTSP_NETSURVEILLANCE_91_URL` | URL NVR untuk mengambil alamat dan kredensial kontrol; wajib diisi. URL RTSP ini tidak dikirim ke frontend. |
| `PTZ_NVR_CHANNEL` | Indeks channel DVRIP (default `1`, channel D02 pada instalasi ini). |
| `PTZ_CENTER_PRESET` | Nomor preset tengah yang **sudah disimpan** di NVR, `1`–`126`. Kosongkan jika belum ada. |
| `PTZ_API_HOST` | Alamat bind, default `127.0.0.1`. |
| `PTZ_API_PORT` | Port HTTP, default `8091`. |
| `PTZ_API_KEY` | Kosong untuk akses lokal saja; wajib diisi bila bind ke alamat non-lokal. |

Jalankan dari folder `ptz_api`:

```bash
python3 api.py
```

Atau dari folder induk: `python3 ptz_api/api.py`. Jika port `8091` sedang dipakai, hentikan server lama terlebih dahulu atau jalankan dengan `--port 8093`.

- Swagger UI: `http://127.0.0.1:8091/docs`
- OpenAPI JSON: `http://127.0.0.1:8091/openapi.json`
- Base API: `http://127.0.0.1:8091`

Swagger UI FastAPI memuat aset JavaScript/CSS dari CDN; akses internet diperlukan untuk tampilan Swagger, tetapi endpoint API tetap lokal.

## Endpoint

| Method | Path | Fungsi |
| --- | --- | --- |
| `GET` | `/api/health` | Status server API, tidak menghubungi NVR. |
| `GET` | `/api/ptz/102` | Kesiapan NVR dan kemampuan kontrol, termasuk `lease`, `max_hold`, dan `center_available`. |
| `POST` | `/api/ptz/102` | Kontrol PTZ dengan body JSON (`Content-Type: application/json`). |

Arah yang didukung: `left`, `right`, `up`, `down`, `zoom_in`, `zoom_out`. `speed` valid `0.05`–`0.35` (default `0.15`).

### Tekan-tahan (disarankan untuk frontend)

Kirim `start` **sekali saat tombol ditekan**, bukan sekali per klik atau berulang setiap interval:

```json
{"action":"start","direction":"left","speed":0.15}
```

Respons mengandung `token`, `lease` (detik), dan `max_hold` (detik). Contoh:

```json
{"ok":true,"action":"left","token":"TOKEN_DARI_START","lease":0.9,"max_hold":8.0,"message":"Kamera bergerak selama tombol ditahan."}
```

Selama tombol masih ditekan, kirim `renew` dengan token tersebut sekitar setiap **250 ms**:

```json
{"action":"renew","token":"TOKEN_DARI_START"}
```

Saat dilepas, dibatalkan, tab disembunyikan, atau halaman kehilangan fokus, kirim `stop`:

```json
{"action":"stop","token":"TOKEN_DARI_START"}
```

Jika tidak ada `renew`, watchdog menghentikan gerakan setelah sekitar `0.9` detik. Sekali tahan dibatasi `8` detik; setelah itu lepas dan tekan kembali untuk memulai gerakan baru. Bila `start` baru bertabrakan dengan gerakan lama, API mencoba menghentikan gerakan lama lalu memulai yang baru; responsnya berisi `replaced_previous: true`. Token lama tidak boleh dipakai untuk `renew`; respons konflik `409` berarti frontend harus menghentikan timer dan menghapus token lama. `stop` dengan token yang sudah tidak aktif aman dipanggil ulang.

Contoh minimal integrasi tombol frontend (jalankan pada halaman lokal, misalnya `localhost:5173`):

```js
const endpoint = "http://127.0.0.1:8091/api/ptz/102";
const button = document.querySelector("#ptz-left");
let hold = null;

async function send(command) {
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function release() {
  const current = hold;
  if (!current || current.released) return;
  current.released = true;
  clearInterval(current.timer);
  if (current.token) {
    send({ action: "stop", token: current.token })
      .catch(console.error)
      .finally(() => { if (hold === current) hold = null; });
  }
}

button.addEventListener("pointerdown", async (event) => {
  if (event.button !== 0 || hold) return;
  button.setPointerCapture(event.pointerId);
  const current = { token: null, timer: null, renewing: false, released: false };
  hold = current;
  try {
    const result = await send({ action: "start", direction: "left", speed: 0.15 });
    current.token = result.token;
    if (current.released) {
      await send({ action: "stop", token: current.token });
      return;
    }
    current.timer = setInterval(async () => {
      if (current.released || current.renewing) return;
      current.renewing = true;
      try {
        await send({ action: "renew", token: current.token });
      } catch (error) {
        release();
        console.error(error);
      } finally {
        current.renewing = false;
      }
    }, 250);
  } catch (error) {
    console.error(error);
    current.released = true;
  } finally {
    if (current.released && hold === current) hold = null;
  }
});
button.addEventListener("pointerup", release);
button.addEventListener("pointercancel", release);
button.addEventListener("lostpointercapture", release);
window.addEventListener("blur", release);
window.addEventListener("pagehide", release);
document.addEventListener("visibilitychange", () => { if (document.hidden) release(); });
```

### Gerak singkat dan preset tengah

Untuk satu gerakan pendek tanpa token, set `action` langsung ke arah:

```json
{"action":"right","speed":0.15,"duration":0.35}
```

`duration` valid `0.1`–`0.5` detik (default `0.35`). Gerakan pendek akan berhenti otomatis.

Untuk menuju preset tengah **yang telah dikonfigurasi** pada `PTZ_CENTER_PRESET`:

```json
{"action":"center"}
```

`center` adalah **action**, bukan `direction`. API mengirim `GotoPreset` ke NVR; jalur gerak menuju posisi tengah ditentukan oleh perangkat. Jika preset belum dikonfigurasi, respons `503`.

## Autentikasi dan keamanan

Secara default server hanya bind ke `127.0.0.1`; tanpa API key hanya permintaan ke host lokal yang diizinkan. Frontend lokal di port lain (misalnya `http://localhost:5173`) didukung oleh CORS. Jika `PTZ_API_KEY` diisi, kirim header `X-PTZ-API-Key` pada setiap request API; tombol **Authorize** otomatis tersedia di Swagger. Jangan menaruh API key untuk akses jaringan di JavaScript frontend publik: gunakan backend proxy yang menyimpan key, dan gunakan HTTPS/VPN jika API perlu diakses melalui jaringan. Binding ke alamat non-lokal tanpa key ditolak saat server dimulai.

Jangan menjalankan dashboard/controller PTZ lain untuk kamera yang sama secara bersamaan: dua layanan yang mengirim perintah PTZ dapat saling mengganggu. `/docs` dan `/openapi.json` dapat dibuka tanpa key, tetapi tidak memuat kredensial NVR.
