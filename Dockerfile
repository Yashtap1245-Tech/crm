FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN DEBUG=true python manage.py collectstatic --noinput && useradd --create-home app
USER app
CMD ["sh", "-c", "python manage.py verify_isolation && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 2 --timeout 60 --access-logfile - --access-logformat '%(s)s %(m)s'"]
