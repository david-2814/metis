// CloudFront Function — runs at viewer-request on every cache miss/hit.
// Two jobs:
//   1. 301-redirect www.<APEX> → <APEX> so apex stays canonical.
//   2. Map directory-style URLs to their /index.html origin keys
//      (Astro emits /about/index.html; visitors type /about or /about/).
//
// The __APEX_DOMAIN__ / __WWW_DOMAIN__ tokens are substituted by CDK at
// synth time — see lib/metis-site-stack.ts. Do not edit them by hand.

function handler(event) {
  var request = event.request;
  var host = request.headers.host ? request.headers.host.value : "";
  var uri = request.uri;

  if (host === "__WWW_DOMAIN__") {
    return {
      statusCode: 301,
      statusDescription: "Moved Permanently",
      headers: {
        location: { value: "https://__APEX_DOMAIN__" + uri },
      },
    };
  }

  if (uri.endsWith("/")) {
    request.uri = uri + "index.html";
  } else if (!uri.includes(".")) {
    request.uri = uri + "/index.html";
  }

  return request;
}
