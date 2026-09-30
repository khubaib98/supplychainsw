FROM postgres:16-alpine
# tar/gzip come from busybox; openssl encrypts, rclone copies off-site.
RUN apk add --no-cache openssl rclone
COPY backup.sh /usr/local/bin/backup.sh
RUN chmod 755 /usr/local/bin/backup.sh
ENV RCLONE_CONFIG=/config/rclone/rclone.conf
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["loop"]
