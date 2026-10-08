import { useEffect, useRef, useState } from 'react';
import { audioExtensions, describeError, formatBytes, jobIdFromHash, parseMediaLink, pollingRetryDelay, pollingRetryLimit, request } from './api.js';
import Waveform from './Waveform.jsx';

const backendWaitingMessage = 'Menunggu server backend… Jalankan .\\server.ps1 jika belum berjalan.';

function jobFromAddress() {
  const id = jobIdFromHash(window.location.hash);
  return id ? { id, progress: 0, message: 'Memuat status pemrosesan…' } : null;
}

function RemoverIcon() {
  return <svg viewBox="0 0 40 30" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M2 8h36M2 22h36M7 4v8M13 2v12M19 5v6M25 3v10M31 6v4M7 19v6M13 17v10M19 20v4M25 18v8M31 19v6" /></svg>;
}

function availableDownloadFormats(job, stem) {
  return ['wav', 'mp3'].filter(format => job.downloads == null || job.downloads[stem]?.[format]?.url);
}

function StemPlayer({ stem, job }) {
  const vocal = stem === 'vocals';
  const [volume, setVolume] = useState(1);
  const audio = useRef(null);
  return <article className={`stem-player ${vocal ? 'vocal' : 'music'}`}>
    <div className="stem-label"><strong>{vocal ? 'Vocal' : 'Music'}</strong><label>Volume<input aria-label={`Volume ${vocal ? 'Vocal' : 'Music'}`} type="range" min="0" max="1" step="0.01" value={volume} onChange={event => { const value = Number(event.target.value); setVolume(value); if (audio.current) audio.current.volume = value; }} /></label></div>
    <div className="stem-content"><audio ref={audio} controls preload="metadata" src={job.files[stem]} aria-label={`Putar ${stem}`} /><div className="downloads">{availableDownloadFormats(job, stem).map(format => {
      const file = job.downloads?.[stem]?.[format];
      const url = file?.url || `${job.files[stem].split('?')[0]}?format=${format}`;
      return <a key={format} href={url} download>{format.toUpperCase()} <small>{format === 'wav' ? '24-bit' : '320 kbps'} {formatBytes(file?.bytes)}</small><span aria-hidden="true">↓</span></a>;
    })}</div></div>
  </article>;
}

export default function App() {
  const [source, setSource] = useState('file');
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState('');
  const [quality, setQuality] = useState('balanced');
  const [view, setView] = useState(() => jobFromAddress() ? 'processing' : 'upload');
  const [job, setJob] = useState(jobFromAddress);
  const [error, setError] = useState('');
  const [pollingMessage, setPollingMessage] = useState('');
  const [health, setHealth] = useState(null);
  const [connection, setConnection] = useState('checking');
  const [dragging, setDragging] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const input = useRef(null);
  const activeRequest = useRef(null);
  const submitting = useRef(false);
  const link = parseMediaLink(url);
  const platform = link?.platform ?? null;
  const maxMb = health?.max_upload_mb ?? 300;
  const ready = connection === 'ready' && (source === 'file' ? Boolean(file) : Boolean(platform));
  const mp3Count = job ? ['vocals', 'instrumental'].filter(stem => availableDownloadFormats(job, stem).includes('mp3')).length : 0;
  const resultFormats = mp3Count === 2 ? 'WAV lossless dan MP3' : mp3Count === 1 ? 'WAV lossless · MP3 tersedia untuk satu track' : 'WAV lossless';

  useEffect(() => {
    const controller = new AbortController();
    let timer;
    // Retry until backend is up
    async function check() {
      try {
        const value = await request('/api/health', { signal: controller.signal });
        setHealth(value); setConnection(value.ffmpeg ? 'ready' : 'error');
        if (!value.ffmpeg) setError('FFmpeg belum tersedia. Jalankan .\\server.ps1 -Setup, lalu restart server.');
        else setError(current => current === backendWaitingMessage ? '' : current);
      } catch (err) {
        if (controller.signal.aborted || err.name === 'AbortError') return;
        setConnection('error'); setError(backendWaitingMessage);
        timer = setTimeout(check, 2000);
      }
    }
    check();
    return () => { clearTimeout(timer); controller.abort(); };
  }, []);

  useEffect(() => () => activeRequest.current?.abort(), []);

  useEffect(() => {
    const hash = job?.id ? `#job=${job.id}` : '';
    if (window.location.hash !== hash) window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}${hash}`);
  }, [job?.id]);

  useEffect(() => {
    // Stray drops would leave page
    const ignore = event => event.preventDefault();
    window.addEventListener('dragover', ignore);
    window.addEventListener('drop', ignore);
    return () => { window.removeEventListener('dragover', ignore); window.removeEventListener('drop', ignore); };
  }, []);

  useEffect(() => { window.scrollTo({ top: 0, behavior: 'instant' }); }, [view]);

  useEffect(() => {
    if (view !== 'processing' || !job?.id) return;
    let timer;
    let failures = 0;
    const controller = new AbortController();
    async function poll() {
      try {
        const next = await request(`/api/jobs/${job.id}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        failures = 0;
        setPollingMessage('');
        setJob(next);
        if (next.status === 'completed') setView('result');
        else if (next.status === 'failed') { setError(next.message); setView('error'); }
        else timer = setTimeout(poll, 1200);
      } catch (err) {
        if (controller.signal.aborted || err.name === 'AbortError') return;
        const delay = pollingRetryDelay(err, ++failures);
        if (delay !== null) {
          setPollingMessage(`Koneksi ke server terganggu. Mencoba lagi (${failures}/${pollingRetryLimit})…`);
          timer = setTimeout(poll, delay);
        } else { setError(describeError(err)); setView('error'); }
      }
    }
    poll();
    return () => { clearTimeout(timer); controller.abort(); };
  }, [view, job?.id]);

  function chooseFile(next) {
    if (!next) return;
    if (!audioExtensions.includes(next.name.split('.').pop().toLowerCase())) { setError('Format tidak didukung. Pilih MP3, WAV, FLAC, M4A, AAC, OGG, atau Opus.'); return; }
    if (!next.size) { setError('Berkas audio kosong.'); return; }
    if (next.size > maxMb * 1024 * 1024) { setError(`Ukuran maksimum ${maxMb} MB.`); return; }
    setFile(next); setError('');
  }

  function reset() {
    activeRequest.current?.abort(); submitting.current = false;
    setJob(null); setFile(null); setUrl(''); setError(''); setView('upload');
    if (input.current) input.current.value = '';
  }

  async function process(event) {
    event.preventDefault();
    if (!ready || submitting.current) return;
    submitting.current = true;
    const controller = new AbortController(); activeRequest.current = controller;
    setError(''); setPollingMessage(''); setView('processing');
    setJob({ progress: 0, filename: source === 'file' ? file.name : `${platform === 'youtube' ? 'YouTube' : 'TikTok'} audio`, message: source === 'file' ? 'Mengunggah audio…' : 'Mengambil audio dari link…' });
    try {
      let result;
      if (source === 'file') {
        const form = new FormData(); form.append('file', file); form.append('quality_mode', quality);
        result = await request('/api/jobs', { method: 'POST', body: form, signal: controller.signal });
      } else result = await request('/api/media', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: link.url, quality_mode: quality }), signal: controller.signal });
      if (!controller.signal.aborted) setJob(result);
    } catch (err) { if (err.name !== 'AbortError') { setError(describeError(err)); setView('error'); } }
    finally { submitting.current = false; }
  }

  return <>
    <button className="menu-toggle" aria-label="Buka menu" aria-expanded={menuOpen} onClick={() => setMenuOpen(!menuOpen)}>☰</button>
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`}><nav aria-label="Tools"><a href="#" aria-current="page" onClick={() => setMenuOpen(false)}><RemoverIcon /><span>Remover</span></a></nav><div className="sidebar-bottom"><span className={`status-dot ${connection}`} /><span>LOCAL</span></div></aside>
    <main>
      <section className="landing" aria-labelledby="title">
        <div className="top-menu"><a href="#how-it-works">Cara kerja</a></div>
        <h1 id="title">Vocal Remover and Isolation</h1>
        <p className="subtitle">Pisahkan suara dari musik dalam sebuah lagu<br className="desktop-break" /> dengan bantuan AI.</p>
        {view === 'upload' && <form className="upload-workspace" onSubmit={process}>
          <div className={`wave-preview ${dragging ? 'dragging' : ''}`} role="img" aria-label="Ilustrasi dua track: musik berwarna hijau dan vokal berwarna ungu" onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); setSource('file'); chooseFile(event.dataTransfer.files[0]); }}>
            <div className="preview-row music"><div className="preview-label">Music <span className="volume-mark" /></div><Waveform /></div>
            <div className="preview-row vocal"><div className="preview-label">Vocal <span className="volume-mark" /></div><Waveform kind="vocal" /></div>
            <div className="preview-controls"><span>▶</span><span>00:00 <b>/</b> 04:32</span><div className="preview-line" /><span>♫</span></div>
          </div>
          <div className="source-tabs" aria-label="Sumber audio">{[['file', 'File audio'], ['link', 'YouTube / TikTok']].map(([value, label]) => <button key={value} type="button" aria-pressed={source === value} className={source === value ? 'active' : ''} onClick={() => { setSource(value); setError(''); }}>{label}</button>)}</div>
          <input ref={input} hidden type="file" accept={audioExtensions.map(ext => `.${ext}`).join(',')} onChange={event => { chooseFile(event.target.files[0]); event.target.value = ''; }} />
          {source === 'file' ? <><button className="browse-button" type="button" onClick={() => input.current.click()}>Pilih file saya</button><p className="input-hint">atau seret file ke waveform · maks. {maxMb} MB</p>{file && <div className="selected-file"><span><strong>{file.name}</strong><small>{formatBytes(file.size)}</small></span><button type="button" aria-label="Hapus file pilihan" onClick={() => setFile(null)}>×</button></div>}</> : <div className="link-input"><label htmlFor="media-url">Link video YouTube atau TikTok</label><div><input id="media-url" type="text" inputMode="url" autoComplete="off" spellCheck={false} value={url} placeholder="https://youtu.be/… atau https://vm.tiktok.com/…" onChange={event => setUrl(event.target.value)} /><button type="button" onClick={async () => { try { setUrl((await navigator.clipboard.readText()).trim()); } catch { document.getElementById('media-url').focus(); } }}>Tempel</button></div><p className="input-hint">{platform ? `${platform === 'youtube' ? 'YouTube' : 'TikTok'} terdeteksi · maks. ${platform === 'youtube' ? health?.max_youtube_minutes ?? 30 : health?.max_tiktok_minutes ?? 15} menit` : url ? 'Tempel link satu video publik — bukan playlist, channel, atau profil.' : `Satu video publik · YouTube ${health?.max_youtube_minutes ?? 30} menit / TikTok ${health?.max_tiktok_minutes ?? 15} menit`}</p></div>}
          <fieldset className="quality-picker">
            <legend>Kualitas pemisahan</legend>
            {[
              ['balanced', 'Balanced', 'Lebih cepat'],
              ['ultra', 'Ultra Human Focus', 'Lebih bersih · jauh lebih lama'],
            ].map(([value, label, hint]) => (
              <label key={value} className={`quality-option ${quality === value ? 'selected' : ''}`}>
                <input type="radio" name="quality" value={value} checked={quality === value} onChange={() => setQuality(value)} />
                <span className="quality-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                    {value === 'balanced'
                      ? <path d="M13 3 5 13h6l-1 8 9-11h-6l1-7Z" />
                      : <><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z" /><path d="M20 2v4m-2-2h4" /></>}
                  </svg>
                </span>
                <span className="quality-check" aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m4 8 2.5 2.5L12 5" /></svg></span>
                <span className="quality-copy"><strong>{label}</strong><small>{hint}</small></span>
              </label>
            ))}
          </fieldset>
          {error && <p className="error-message" role="alert">{error}</p>}
          <button className="process-button" type="submit" disabled={!ready}>{connection === 'checking' ? 'Menghubungkan…' : 'Pisahkan audio'}</button>
          <p className="privacy-note">Audio diproses di komputer kamu.</p>
        </form>}
        {view === 'processing' && <div className="processing" role="status" aria-live="polite"><div className="wave-loader">{Array.from({ length: 9 }, (_, i) => <i key={i} />)}</div><h2>Memisahkan audio…</h2><p>{pollingMessage || job?.message}</p><div className="progress-track" role="progressbar" aria-label="Progres pemisahan" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, Math.max(0, job?.progress ?? 0))}><span style={{ width: `${Math.min(100, Math.max(0, job?.progress ?? 0))}%` }} /></div><div className="progress-meta"><span>{job?.filename}</span><b>{job?.progress ?? 0}%</b></div><p className="input-hint">{(job?.quality_mode ?? quality) === 'ultra' ? 'Mode Ultra membutuhkan waktu lebih lama pada CPU.' : 'Lama pemrosesan tergantung durasi audio dan komputer kamu.'}</p></div>}
        {view === 'error' && <div className="error-panel" role="alert"><h2>Audio belum berhasil dipisahkan</h2><p>{error}</p><button className="browse-button" onClick={() => { setView('upload'); setJob(null); setError(''); }}>Coba lagi</button></div>}
        {view === 'result' && job && <div className="results"><div className="result-header"><div><h2>Track kamu sudah siap</h2><p>{job.filename}</p></div><button className="browse-button" onClick={reset}>Lagu baru</button></div>{job.warnings?.map((warning, index) => <p className="input-hint" role="status" key={index}><strong>Catatan:</strong> {warning}</p>)}<StemPlayer stem="instrumental" job={job} /><StemPlayer stem="vocals" job={job} />{job.analysis && <div className="analysis"><span>BPM <strong>{job.analysis.bpm || '—'}</strong></span><span>Key <strong>{job.analysis.key || '—'}{job.analysis.key_chord && job.analysis.key_chord !== '—' ? ` / ${job.analysis.key_chord}` : ''}</strong><small>{job.analysis.key_confidence ? `${job.analysis.key_confidence}% confidence` : ''}</small></span><span>Chord <strong>{job.analysis.chords?.join(' · ') || '—'}</strong></span></div>}<p className="input-hint">{job.separation_label} · {resultFormats}{job.analysis ? ' · Key dan chord adalah estimasi.' : ''}</p></div>}
      </section>
      <section id="how-it-works" className="info"><h2>Hapus vokal dari sebuah lagu</h2><div className="info-copy"><p>Pilih lagu dari komputer atau tempel link video YouTube maupun TikTok. AI akan memisahkan suara penyanyi dari musiknya.</p><p>Kamu akan mendapatkan dua track: instrumental untuk karaoke dan vokal terisolasi untuk acapella. Dengarkan hasilnya, lalu unduh dalam format WAV atau MP3.</p><p>Pilih Balanced untuk pemrosesan lebih cepat, atau Ultra Human Focus untuk pemisahan yang lebih bersih. Semua proses berjalan lokal; model akan diunduh saat pertama kali digunakan.</p></div><footer>VOCALIFT · LOCAL AUDIO TOOL</footer></section>
    </main>
  </>;
}
