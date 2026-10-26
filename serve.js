const http = require("http");
const fs = require("fs");
const path = require("path");

const root = __dirname;
const types = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

http
  .createServer((req, res) => {
    const rel = decodeURIComponent(req.url.split("?")[0]);
    const file = path.join(root, rel === "/" ? "harness.html" : rel);

    if (!file.startsWith(root)) {
      res.writeHead(403).end("no");
      return;
    }

    fs.readFile(file, (err, data) => {
      if (err) {
        res.writeHead(404).end("not found");
        return;
      }
      res.writeHead(200, {
        "Content-Type": types[path.extname(file)] || "application/octet-stream",
        "Cache-Control": "no-store",
      });
      res.end(data);
    });
  })
  .listen(8765, () => console.log("serving on http://localhost:8765"));
