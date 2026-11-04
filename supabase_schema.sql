-- ============================================================================
-- ANTIGRAVITY STUDIO - SUPABASE TAM VERİTABANI VE STORAGE ŞEMASI
-- Supabase Dashboard -> SQL Editor ekranına yapıştırıp "Run" ile çalıştırın.
-- ============================================================================

-- 1. DOCUMENT HUB: Belge & Ödev Dönüştürme Geçmişi Tablosu
create table if not exists public.document_history (
  id uuid default gen_random_uuid() primary key,
  file_name text not null,
  file_type text not null, -- 'pdf' veya 'docx'
  file_size_mb numeric,
  file_url text not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

alter table public.document_history enable row level security;

create policy "Herkese açık belge geçmişi okuma"
  on public.document_history for select
  using ( true );

create policy "Herkese açık belge geçmişi ekleme"
  on public.document_history for insert
  with check ( true );


-- 2. MEDIA DOWNLOADER: İndirilen Medya (MP4 / MP3 / Kapak) Kayıt Tablosu
create table if not exists public.media_history (
  id uuid default gen_random_uuid() primary key,
  source_url text not null,
  title text,
  media_mode text not null default 'video', -- 'video' (mp4), 'audio' (mp3), 'thumbnail' (jpg)
  platform text, -- 'youtube' veya 'instagram'
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

alter table public.media_history enable row level security;

create policy "Herkese açık medya geçmişi okuma"
  on public.media_history for select
  using ( true );

create policy "Herkese açık medya geçmişi ekleme"
  on public.media_history for insert
  with check ( true );


-- 3. WHISPER AUDIO ASSISTANT: Sesli Ders & Toplantı Transkript Tablosu
create table if not exists public.audio_transcriptions (
  id uuid default gen_random_uuid() primary key,
  file_name text not null,
  transcript_text text not null,
  summary_bullets jsonb default '[]'::jsonb,
  model_used text default 'whisper-large-v3',
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

alter table public.audio_transcriptions enable row level security;

create policy "Herkese açık ses transkripti okuma"
  on public.audio_transcriptions for select
  using ( true );

create policy "Herkese açık ses transkripti ekleme"
  on public.audio_transcriptions for insert
  with check ( true );


-- 4. STORAGE BUCKET: Taranan PDF ve Word Dosyaları İçin Depolama Alanı
insert into storage.buckets (id, name, public)
values ('student_documents', 'student_documents', true)
on conflict (id) do nothing;

create policy "Herkese açık storage okuma"
  on storage.objects for select
  using ( bucket_id = 'student_documents' );

create policy "Herkese açık storage yükleme"
  on storage.objects for insert
  with check ( bucket_id = 'student_documents' );
