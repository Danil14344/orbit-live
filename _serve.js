const http = require('http');
const fs = require('fs');
const path = require('path');
const ROOT = __dirname;
const PORT = 8000;
const TYPES = {'.html':'text/html; charset=utf-8','.js':'application/javascript','.css':'text/css','.png':'image/png','.glb':'model/gltf-binary','.json':'application/json'};
http.createServer((req,res)=>{
  let p = decodeURIComponent(req.url.split('?')[0]);
  if (p === '/') p = '/eye_demo.html';
  const fp = path.join(ROOT, p);
  if (!fp.startsWith(ROOT)) { res.writeHead(403); return res.end('forbidden'); }
  fs.readFile(fp, (err, data)=>{
    if (err) { res.writeHead(404); return res.end('not found'); }
    res.writeHead(200, {'Content-Type': TYPES[path.extname(fp).toLowerCase()] || 'application/octet-stream', 'Access-Control-Allow-Origin':'*'});
    res.end(data);
  });
}).listen(PORT, '127.0.0.1', ()=>console.log('http://127.0.0.1:'+PORT+'/eye_demo.html'));
