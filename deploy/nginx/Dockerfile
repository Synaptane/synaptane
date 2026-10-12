# nginx for the all-in-one mode ("proxy" profile of compose.yaml).
# Built from the root of the repository, for the license files (files allowed:
# Dockerfile.dockerignore next to this file):
#   docker build -f deploy/nginx/Dockerfile -t synaptane-nginx:local .
FROM nginx:1.31-alpine@sha256:df221db836e1754089190208cee7eeda94f233197056426eda74a43ab1abeac2
RUN apk add --no-cache openssl
# Licenses: Synaptane's, and the third-party notices (nginx, Alpine, OpenSSL: "nginx proxy
# image" section).
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/doc/synaptane/
COPY deploy/nginx/synaptane.conf.template /etc/synaptane/nginx.conf.template
COPY deploy/nginx/start.sh /start.sh
ENTRYPOINT ["/bin/sh", "/start.sh"]
