# A review page served over HTTP, so that a stranger can stand in front of the same page
# Victor demos without cloning anything, without installing the skill, and without the
# `file://` caveats the zip download carries: a page opened off disk cannot fetch its own
# `content.json` under a strict browser, and every fetch it does make is a cross-origin
# one. Behind a server it is simply the page.
#
# The build context is the repository root, because the snapshots are committed verbatim
# under demo/ and there is nothing to generate. `publish-demo.sh` has already dropped
# `*.raw.webm` and everything the run kept for itself, so the directory copied here is
# exactly the directory Pages deploys.
FROM nginx:1.27-alpine

# Which committed directory becomes the site root. The default is demo/ itself, whose
# index.html is the hand-written landing page listing every snapshot — that is the `latest`
# image, the whole gallery. A per-snapshot image passes demo/<slug> and gets that one
# review page at `/`, which is what makes the tag readable as "the artifact of that review".
ARG SNAPSHOT=demo

# What links the package to this repository in GitHub's UI. Without it the image is
# published but orphaned: it never appears in the repo's Packages sidebar and its page
# carries no source link back here.
LABEL org.opencontainers.image.source="https://github.com/victorrentea/human-review"
LABEL org.opencontainers.image.description="A /human-review page, served as-is. Open http://localhost:8642 after `docker run -p 8642:80`."
LABEL org.opencontainers.image.licenses="MIT"

COPY .github/snapshot.nginx.conf /etc/nginx/conf.d/default.conf
COPY ${SNAPSHOT}/ /usr/share/nginx/html/
