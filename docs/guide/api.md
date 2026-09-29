---
hide:
  - navigation
  - toc
---

# API reference

Editions: OSS, Cloud, Enterprise. The reference below is the OpenAPI schema served by preloop.ai; a self-hosted server serves its own at `/api/v1/openapi.json`. Endpoints that need a Cloud or Enterprise plugin return 402 or 404 on OSS.

<div id="redoc-container"></div>

<script>
  function initRedoc() {
    Redoc.init(
      'https://preloop.ai/api/v1/openapi.json',
      {
        scrollYOffset: 50,
        hideDownloadButton: false,
        theme: {
          colors: {
            primary: {
              main: '#58a6ff'
            },
            text: {
              primary: '#e6edf3',
              secondary: '#8b949e'
            },
            gray: {
              50: '#161b24',
              100: '#212632'
            }
          },
          typography: { fontSize: "16px",
            fontFamily: 'Roboto, -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif',
            headings: {
              fontFamily: 'Roboto, -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif',
              color: '#e6edf3'
            },
            code: {
              fontFamily: 'Fira Code, monospace',
              color: '#e6edf3'
            }
          },
          sidebar: {
            backgroundColor: 'rgb(33, 38, 50)',
            textColor: '#e6edf3',
            arrow: {
              color: '#58a6ff'
            }
          },
          rightPanel: {
            backgroundColor: 'rgb(33, 38, 50)',
            textColor: '#e6edf3'
          },
          codeBlock: {
            backgroundColor: '#0d1117'
          }
        }
      },
      document.getElementById('redoc-container')
    );
  }
</script>
<script src="https://cdn.jsdelivr.net/npm/redoc@2.5.4/bundles/redoc.standalone.js"
        integrity="sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5"
        crossorigin="anonymous" onload="initRedoc()"></script>

<style>
  #redoc-container {
    background: rgb(33, 38, 50);
  }
</style>
