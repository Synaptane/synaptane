# nginx for the all-in-one mode ("proxy" profile of compose.yaml).
FROM nginx:1.31-alpine@sha256:df221db836e1754089190208cee7eeda94f233197056426eda74a43ab1abeac2
RUN apk add --no-cache openssl
COPY synaptane.conf.template /etc/synaptane/nginx.conf.template
COPY start.sh /start.sh
ENTRYPOINT ["/bin/sh", "/start.sh"]
