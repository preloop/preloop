/* macOS acceptance fixture only. Redirect this child process's managed reads
 * into its scratch home; never replace any real machine policy. The vendor
 * executable code is unchanged; its scratch copy needs an ad-hoc signature
 * to permit dyld interposition. Not a deployment or bypass-resistance test.
 */
#define _DARWIN_C_SOURCE
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
static const char *redirect(const char *path, char *buffer) {
  const char *base = "/Library/Application Support/ClaudeCode";
  const char *root = getenv("PRELOOP_TEST_MANAGED_ROOT");
  if (root && path && strncmp(path, "/Library/Managed Preferences/", 29) == 0) {
    snprintf(buffer, 4096, "%s/absent-preferences", root);
    return buffer;
  }
  if (root && path && strncmp(path, base, strlen(base)) == 0 &&
      (path[strlen(base)] == '/' || path[strlen(base)] == 0)) {
    snprintf(buffer, 4096, "%s%s", root, path + strlen(base));
    return buffer;
  }
  return path;
}
#define INTERPOSE(newf, oldf)                                                          \
  __attribute__((used)) static struct {                                                \
    const void *a;                                                                     \
    const void *b;                                                                     \
  } interpose_##oldf __attribute__((section("__DATA,__interpose"))) = {                \
      (const void *)&newf, (const void *)&oldf};
static int redirected_open(const char *p, int flags, ...) {
  mode_t mode = 0;
  if (flags & O_CREAT) {
    va_list ap;
    va_start(ap, flags);
    mode = va_arg(ap, int);
    va_end(ap);
  }
  char b[4096];
  return open(redirect(p, b), flags, mode);
}
static int redirected_stat(const char *p, struct stat *s) {
  char b[4096];
  return stat(redirect(p, b), s);
}
static int redirected_lstat(const char *p, struct stat *s) {
  char b[4096];
  return lstat(redirect(p, b), s);
}
static int redirected_access(const char *p, int mode) {
  char b[4096];
  return access(redirect(p, b), mode);
}
static DIR *redirected_opendir(const char *p) {
  char b[4096];
  return opendir(redirect(p, b));
}
static char *redirected_realpath(const char *p, char *r) {
  char b[4096];
  return realpath(redirect(p, b), r);
}
INTERPOSE(redirected_open, open)
INTERPOSE(redirected_stat, stat)
INTERPOSE(redirected_lstat, lstat)
INTERPOSE(redirected_access, access)
INTERPOSE(redirected_opendir, opendir)
INTERPOSE(redirected_realpath, realpath)
static int redirected_openat(int d, const char *p, int flags, ...) {
  mode_t mode = 0;
  if (flags & O_CREAT) {
    va_list ap;
    va_start(ap, flags);
    mode = va_arg(ap, int);
    va_end(ap);
  }
  char b[4096];
  return openat(d, redirect(p, b), flags, mode);
}
static int redirected_fstatat(int d, const char *p, struct stat *s, int flags) {
  char b[4096];
  return fstatat(d, redirect(p, b), s, flags);
}
static int redirected_faccessat(int d, const char *p, int m, int flags) {
  char b[4096];
  return faccessat(d, redirect(p, b), m, flags);
}
INTERPOSE(redirected_openat, openat)
INTERPOSE(redirected_fstatat, fstatat)
INTERPOSE(redirected_faccessat, faccessat)
extern int open_nocancel(const char *, int, ...) __asm("_open$NOCANCEL");
extern int openat_nocancel(int, const char *, int, ...) __asm("_openat$NOCANCEL");
INTERPOSE(redirected_open, open_nocancel)
INTERPOSE(redirected_openat, openat_nocancel)
