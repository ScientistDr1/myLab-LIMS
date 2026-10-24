FROM python:3.12-slim
RUN useradd -m lims && mkdir /data && chown lims /data
WORKDIR /app
COPY server.py .
COPY public ./public
ENV LIMS_DATA=/data PYTHONUNBUFFERED=1
USER lims
VOLUME /data
EXPOSE 8080
CMD ["python", "server.py"]
