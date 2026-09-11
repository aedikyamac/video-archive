# Video Archive

Frontend for a personal video archive.

Planned architecture:
- Vercel hosts the frontend and lightweight metadata API.
- Object storage holds video bytes; large uploads use direct/resumable uploads.
- Metadata stores source URL, title, thumbnail, duration, archive timestamp, object key, and status.
- A separately authorized worker performs downloads only for content the owner has permission to archive.
- Chat-link ingestion requires a supported webhook/event source; the frontend does not scrape or download platform content by itself.

Environment variables and storage credentials must be configured in Vercel, never committed to this repository.
