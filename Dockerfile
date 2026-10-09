FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home app
COPY . .
RUN mkdir -p /app/data && chown app:app /app/data
USER app
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8000/health',headers={'Host':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(os.environ['PUBLIC_BASE_URL']).netloc}); urllib.request.urlopen(r,timeout=3)"
CMD ["python", "server.py"]
