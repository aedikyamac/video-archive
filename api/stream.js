export const config = { runtime: 'edge' };

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Range, Content-Type',
};

function responseHeaders(source) {
  const headers = new Headers(cors);
  headers.set('Accept-Ranges', 'bytes');
  headers.set('Content-Type', source.headers.get('content-type') || 'video/mp4');
  for (const name of ['content-range', 'content-length', 'etag', 'last-modified']) {
    const value = source.headers.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}

async function driveFetch(id, range) {
  const url = `https://drive.usercontent.google.com/download?id=${encodeURIComponent(id)}&export=download`;
  const headers = range ? { Range: range } : {};
  let response = await fetch(url, { headers, redirect: 'follow' });
  const type = response.headers.get('content-type') || '';
  if (type.includes('text/html')) {
    const html = await response.text();
    const token = html.match(/confirm=([0-9A-Za-z_-]+)/)?.[1] || html.match(/name="confirm" value="([^"]+)"/)?.[1];
    const cookie = response.headers.get('set-cookie')?.split(';')[0];
    if (token) {
      const confirmUrl = `${url}&confirm=${encodeURIComponent(token)}`;
      response = await fetch(confirmUrl, { headers: { ...(range ? { Range: range } : {}), ...(cookie ? { Cookie: cookie } : {}) }, redirect: 'follow' });
    } else {
      return new Response(html, { status: 502, headers: { ...cors, 'Content-Type': 'text/plain' } });
    }
  }
  return response;
}

export default async function handler(req) {
  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
  if (req.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: cors });
  const id = new URL(req.url).searchParams.get('id');
  if (!id || !/^[\w-]+$/.test(id)) return new Response('Missing or invalid Drive file id', { status: 400, headers: { ...cors, 'Content-Type': 'text/plain' } });
  try {
    const source = await driveFetch(id, req.headers.get('range'));
    const headers = responseHeaders(source);
    return new Response(source.body, { status: source.status === 206 ? 206 : 200, headers });
  } catch (error) {
    return new Response(`Upstream Drive error: ${error.message}`, { status: 502, headers: { ...cors, 'Content-Type': 'text/plain' } });
  }
}
