---
hide:
  - navigation
  - toc
---

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
<script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js" onload="initRedoc()"> </script>

<style>
  #redoc-container {
    background: rgb(33, 38, 50);
  }
</style>
