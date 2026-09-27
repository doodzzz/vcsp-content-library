# Reproducible build on the same base image as Broadcom's plug-in template:
#   docker build --build-arg PORTAL_URL=https://vcsp.example.local --output type=local,dest=out .
FROM node:18.20-slim AS build
WORKDIR /usr/app
COPY . .
ARG PORTAL_URL
ARG VERSION=
RUN ./package.sh --portal-url "$PORTAL_URL" ${VERSION:+--version "$VERSION"}

FROM scratch AS export
COPY --from=build /usr/app/library-upload-plugin-*.zip* /
