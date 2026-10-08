# Vocalift — Vocal Remover Lokal

Aplikasi web lokal untuk memisahkan lagu menjadi **vocals** dan **instrumental** menggunakan model spesialis vokal dari ensemble HTDemucs fine-tuned. Audio dapat diunggah dari komputer atau diambil dari link **YouTube maupun TikTok** — platform dikenali otomatis dari link yang ditempel. Semua diproses lokal. Hasilnya tersedia dalam dua format: **WAV 24-bit** untuk dimix ulang, dan MP3 320 kbps untuk didengarkan atau dibagikan.

Model default `04573f0d` adalah checkpoint khusus vocal yang dipakai oleh `htdemucs_ft`. Instrumental dibuat dengan mengurangi prediksi vocal dari audio asli (`mixture - vocals`), sehingga detail musik lebih konsisten dan kebocoran melodi pada stem vocal lebih rendah dibanding model HTDemucs standar.

Aplikasi menyediakan dua mode pemisahan:

- **Balanced** menjalankan Demucs lalu `Kim_Vocal_2.onnx` sebagai cross-check. Mask spektral konservatif mempertahankan komponen vocal yang disepakati kedua model, sementara bagian yang ditekan dikembalikan ke instrumental. Jika model cross-check gagal, aplikasi otomatis memakai hasil Demucs agar job tetap selesai.
- **Ultra Human Focus** menjalankan preset `vocal_clean`: BS-RoFormer Vocals Revive V2 dan MelBand RoFormer Kim FT2 Bleedless, lalu menggabungkan keduanya dengan `min_fft`. Mode ini paling agresif mengurangi instrument bleed, tetapi jauh lebih lambat pada CPU. Model besar diunduh otomatis ketika Ultra pertama kali dipakai.

## Pembersihan instrumental

Instrumental tidak lagi sekadar sisa pengurangan `mixture - vocals`. Pengurangan
itu mewariskan setiap kesalahan model vokal ke musik, dan itulah yang terdengar
sebagai bleed vokal dan gema hantu. Tiga tahap dijalankan, semuanya DSP tanpa
pass model tambahan:

1. **Pengurangan eksak di domain waktu.** Stem vokal hasil refinement dikurangi
   apa adanya, sehingga fase tetap benar dan kedua stem masih menjumlah kembali
   menjadi mixture. Di mode Balanced, area yang diperdebatkan Demucs dan Kim
   dikembalikan ke musik, karena di situ biasanya instrumen yang salah dilabeli.
2. **Pembatalan ekor reverb secara koheren.** Ekor reverb adalah vokal yang
   dikonvolusi ruangan — fungsi linear dari stem vokal yang sudah kita punya.
   Filter ruangan itu dicocokkan per frekuensi dengan least squares lalu
   dikurangkan dengan fase yang benar. Mask bernilai real tidak bisa melakukan
   ini: kalau gema dan musik menempati bin yang sama, mask hanya bisa mengecilkan
   keduanya sekaligus.
3. **Ducking bleed dari pendapat kedua.** Di mode Balanced, bagian yang menurut
   Kim masih vokal tetapi terlewat Demucs ikut ditekan, dengan batas bawah
   supaya ketidaksepakatan dua model tidak melubangi musik.

Tahap 2 hanya bekerja bila gema memang terukur menonjol di atas lantai musik
band tersebut. Pengaman ini penting: stem vokal selalu membawa sedikit bleed
instrumen, dan tanpa syarat itu filter akan "memprediksi" musik dari bleed
tersebut lalu ikut menguranginya. Ambience alami musik sendiri (room drum,
reverb gitar) tidak disentuh — yang dihapus hanya ekor yang mengikuti vokal.

## Format hasil

Setiap stem dirender sekali ke **WAV 24-bit 44,1 kHz**, lalu salinan MP3 320 kbps
dibuat dari WAV itu. Di halaman hasil ada dua tombol per stem — WAV dan MP3 —
lengkap dengan ukuran berkasnya, karena WAV kira-kira sepuluh kali lebih besar.
Pemutar di halaman memakai MP3 supaya cepat dimuat.

Jika pembuatan salinan MP3 gagal, hasil tetap selesai dengan WAV yang sudah
berhasil dirender. Pemutar memakai WAV untuk track tersebut, tombol unduhan
hanya menampilkan format yang tersedia, dan halaman menjelaskan MP3 yang gagal.
Kegagalan menghapus file sementara yang terkunci juga tidak membatalkan hasil.

Kenapa 24-bit dan bukan float32: limiter di akhir rantai mastering sudah menahan
sinyal di −1,5 dBTP, jadi tidak ada yang perlu dipotong, dan 24 bit menaruh noise
floor ~144 dB di bawah — jauh di bawah apa pun yang tersisa dari proses separasi.
24-bit juga yang diharapkan DAW saat stem dimix ulang.

MP3 di-encode dari WAV yang sudah selesai, bukan dengan menjalankan ulang rantai
mastering, supaya kedua berkas identik kecuali codec-nya.

Endpoint: `GET /api/jobs/{id}/files/{stem}?format=wav` (default `mp3`).

Soal disk: WAV 24-bit memakai ~16 MB per menit per stem, jadi lagu 4 menit
menghasilkan sekitar 145 MB untuk keempat berkas. Semuanya ikut dibersihkan
setelah `RESULT_TTL_HOURS`.

Setiap hasil melewati tahap mastering otomatis setelah separasi:

- Vocals: high-pass ringan, spectral noise reduction, de-esser lembut, dynamic leveling, compression, normalisasi ke −16 LUFS, dan limiter −1.5 dB. Dynamic leveling punya threshold, jadi jeda antar-frasa tidak ikut diangkat — ini yang dulu membuat noise floor terdengar bernapas.
- Instrumental: high-pass 28 Hz untuk membuang rumble subsonik, denoise ringan, dynamic leveling dan compression ringan, normalisasi ke −14 LUFS, lalu limiter −1.5 dB.
- Loudness dianalisis dengan metode EBU R128 dua tahap agar hasil antar-track lebih konsisten.

Setelah separation, stem instrumental juga dianalisis untuk menampilkan estimasi BPM, key/scale, simbol chord key, confidence, dan empat chord dominan. Hasil key/chord bersifat estimasi dan bisa kurang akurat pada lagu yang sering modulasi atau memakai tuning non-standar.

## Menjalankan di Windows

Frontend memakai React + Vite dan seluruh sumbernya ada di `frontend/`.
Backend FastAPI ada di `app/`; backend hanya melayani API. Frontend berjalan
pada port 5173 dan meneruskan `/api` (termasuk audio dan unduhan) ke backend
pada port 8000 melalui proxy Vite.

Pasang Python 3.11+, Node.js 20.19+ atau 22.12+, dan Windows Terminal.
Semua pengoperasian Windows memakai satu script PowerShell:

```powershell
.\server.ps1 -Setup        # pasang dependensi Python, frontend, dan FFmpeg
.\server.ps1               # buka dua tab PowerShell di Windows Terminal + browser
.\server.ps1 -Dev           # reload backend dan hot reload frontend
.\server.ps1 -NoBrowser     # jalankan tanpa membuka browser
.\server.ps1 -Stop          # hentikan kedua proses beserta anaknya
.\server.ps1 -Build         # build React ke frontend/dist
```

Jika kebijakan PowerShell memblokir script, jalankan tanpa mengubah kebijakan permanen:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\server.ps1
```

Untuk port lain:

```powershell
.\server.ps1 -Dev -Port 8080 -FrontendPort 5174
```

Buka `http://127.0.0.1:5173`, pilih file atau tempel link video, pilih kualitas,
lalu klik **Pisahkan audio**. Launcher memilih PowerShell secara eksplisit;
profil Terminal default yang memakai CMD tidak memengaruhinya. Frontend selalu
memakai Vite dengan hot reload; `-Dev` juga mengaktifkan reload backend.

`-Stop` hanya menghentikan proses yang dicatat launcher proyek ini di `.runtime/`,
bukan sembarang aplikasi yang memakai port. Tutup server lama yang dijalankan
secara manual sebelum start jika portnya sama. Satu worker backend digunakan
karena status dan antrean job tersimpan dalam memori.

Job yang sudah selesai juga dicatat ke `data/results/<id>/job.json`, jadi tombol
unduhan tetap jalan setelah backend di-restart (termasuk reload otomatis `-Dev`)
selama hasilnya belum melewati `RESULT_TTL_HOURS`. Job yang masih diproses saat
backend restart memang terputus; halaman akan mengatakan itu, bukan "job tidak
ditemukan". ID job disimpan di alamat halaman (`#job=…`), sehingga refresh
browser melanjutkan pemrosesan atau membuka hasil yang sama.

Membuka `http://127.0.0.1:8000` langsung akan diarahkan ke UI React di port
frontend.

Acuan HTML dan warna UI dari vocalremover.org ada di `frontend/reference/`.
UI memakai satu tool Remover, waveform hijau/ungu, dan latar gelap dari acuan.

## Sumber dari link

Satu kolom untuk keduanya: tempel link, platform dikenali sendiri dari hostname
dan bentuk path-nya, lalu ikon serta batas durasi di halaman ikut menyesuaikan.

Audio yang diunduh **tidak di-encode ulang**. YouTube dan TikTok sudah menyajikan
audio lossy (Opus/AAC); mengubahnya menjadi MP3 dulu berarti generasi kedua
kehilangan kualitas tanpa manfaat apa pun, karena semua model di belakang toh
men-decode ke PCM. Stream itu di-decode sekali ke WAV float32 dan langsung
dipisahkan. Terukur: re-encode ke MP3 320 menambah error 22 dB dibanding
decode langsung.

float32, bukan 16-bit: decoder lossy merekonstruksi puncak di atas skala penuh
pada master yang keras — pada uji di sini sampai +1,6 dBFS — dan 16-bit akan
memotong semuanya.

Konsekuensinya WAV sementara itu besar (~23 MB per menit audio). File ini
dihapus otomatis begitu pemisahan selesai, jadi tidak menumpuk.

**Catatan:** mengubah MP3 yang *sudah kamu punya* menjadi WAV tidak menambah
kualitas apa pun — informasi yang dibuang encoder MP3 tidak bisa dikembalikan.
Terukur: MP3 yang di-decode langsung dan MP3 yang diubah ke WAV dulu berbeda
−90 dB, yaitu identik. Karena itu file upload diproses apa adanya.

| Platform | Bentuk link yang diterima |
|---|---|
| YouTube | `youtube.com/watch?v=…`, `youtu.be/…`, `youtube.com/shorts/…`, `youtube.com/live/…`, `music.youtube.com`, `m.youtube.com` |
| TikTok | `tiktok.com/@user/video/…`, `tiktok.com/t/…`, `vm.tiktok.com/…`, `vt.tiktok.com/…` |

Link boleh ditempel tanpa `https://`, dan teks share seperti
`Judul lagu https://youtu.be/…` juga diterima — link-nya diambil dari teks.
Link YouTube disusun ulang menjadi `watch?v=ID`, jadi parameter `list=` dari
Mix/playlist dibuang dan hanya video itu yang diunduh.

Link profil TikTok (`tiktok.com/@user` tanpa video) serta playlist, channel, dan
beranda YouTube ditolak di depan, karena itu daftar video dan bukan satu video. Post foto/slideshow diterima tetapi bisa gagal
bila memang tidak punya audio — pesan errornya menjelaskan itu.

Endpoint: `POST /api/media` dengan body `{"url": "...", "quality_mode": "balanced"}`.
`POST /api/youtube` masih ada sebagai alias supaya skrip lama tetap jalan.

Pada proses pertama, model AI yang dipilih akan diunduh. Pemisahan memakai CPU; mode Balanced bisa memerlukan beberapa menit, sedangkan Ultra dapat memerlukan puluhan menit untuk sebuah lagu penuh.

## Konfigurasi opsional

Atur environment variable sebelum menjalankan server, atau lewat `.env` di root. Nilai environment yang sudah ada tidak ditimpa:

- `MAX_UPLOAD_MB` — batas ukuran upload, default `300`.
- `RESULT_TTL_HOURS` — umur file sementara, default `24` jam.
- `DEMUCS_MODEL` — nama/checkpoint model Demucs, default `04573f0d` (HTDemucs fine-tuned vocal specialist).
- `MAX_YOUTUBE_MINUTES` — batas durasi video YouTube, default `30` menit.
- `MAX_TIKTOK_MINUTES` — batas durasi video TikTok, default `15` menit.
- `VOCALIFT_FRONTEND_URL` — tujuan redirect saat port backend dibuka langsung, default `http://127.0.0.1:5173`. `server.ps1` mengisinya otomatis dari `-FrontendPort`.

Penyetelan pembersihan instrumental (ubah hanya bila perlu):

- `INSTRUMENTAL_TAIL_STRENGTH` — porsi ekor reverb yang dikurangi, default `1.0`. Isi `0` untuk mematikan tahap ini sepenuhnya.
- `INSTRUMENTAL_TAIL_TAPS` — panjang filter ruangan, default `14` tap (~330 ms).
- `INSTRUMENTAL_TAIL_RIDGE` — regularisasi fit, default `0.05`. Naikkan bila instrumental terdengar berlubang setelah frasa vokal; menaikkannya juga mengurangi efek pembersihan.
- `INSTRUMENTAL_BLEED_FLOOR` — batas bawah ducking bleed, default `0.25` (maksimum −12 dB).
- `VOCAL_MASK_FLOOR` — batas bawah mask konsensus pada stem vokal, default `0.22`.

Format input: MP3, WAV, FLAC, M4A, AAC, OGG, dan Opus. Hasil lama dibersihkan otomatis setelah melewati batas umur saat aplikasi dimulai kembali.

Fitur link hanya menerima satu video publik dari YouTube atau TikTok, bukan playlist atau profil. Gunakan hanya untuk konten yang kamu miliki atau punya izin untuk diproses.

## Pengujian regresi

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
npm --prefix frontend test
npm --prefix frontend run build
```

Pengujian backend memeriksa cleanup reverb, ekspor WAV/MP3 saat refinement gagal
atau file sementara terkunci, fallback WAV saat encoding MP3 gagal, unduhan yang
tetap jalan setelah backend restart, dan pesan error FFmpeg. Model AI tidak dijalankan dalam suite ini. Pengujian
frontend memeriksa retry saat koneksi atau server terganggu; progres tetap
ditampilkan selama maksimal lima percobaan ulang. Error job yang sebenarnya
tetap ditampilkan sebagai kegagalan.

