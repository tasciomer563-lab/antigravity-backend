-- ============================================================================
-- ÖĞRENCİ ASİSTANI - SUPABASE SQL ŞEMASI
-- Supabase Dashboard -> SQL Editor ekranına yapıştırıp "Run" ile çalıştırın.
-- ============================================================================

-- 1. Belge geçmişi tablosu
create table if not exists public.document_history (
  id uuid default gen_random_uuid() primary key,
  file_name text not null,
  file_type text not null, -- 'pdf' veya 'docx'
  file_size_mb numeric,
  file_url text not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null
);

-- Tablo için RLS (Row Level Security) ve anonim okuma/yazma politikaları
alter table public.document_history enable row level security;

create policy "Herkese açık geçmiş okuma"
  on public.document_history for select
  using ( true );

create policy "Herkese açık geçmiş ekleme"
  on public.document_history for insert
  with check ( true );

-- 2. Storage Bucket (Depolama) oluşturma
insert into storage.buckets (id, name, public)
values ('student_documents', 'student_documents', true)
on conflict (id) do nothing;

-- 3. Storage erişim izinleri (anonim yükleme/okuma için açık)
create policy "Herkese açık okuma"
  on storage.objects for select
  using ( bucket_id = 'student_documents' );

create policy "Herkese açık yükleme"
  on storage.objects for insert
  with check ( bucket_id = 'student_documents' );
