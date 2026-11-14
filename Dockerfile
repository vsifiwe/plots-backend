FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY manage.py ./
COPY app ./app
COPY core ./core
COPY deploy/database.py ./deploy/database.py
RUN python manage.py collectstatic --noinput \
    && groupadd --gid 10001 django \
    && useradd --uid 10001 --gid 10001 --no-create-home django \
    && install -d -o django -g django -m 0700 /data

USER django
EXPOSE 8000
CMD ["gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--access-logfile", "-"]
