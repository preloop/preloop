/**
 * Shared test hooks: clear short-TTL API caches between cases so one stub's
 * /features or /users/me response cannot leak into the next test.
 */
import { invalidateApiCaches } from './api.js';

beforeEach(() => {
  invalidateApiCaches();
});

afterEach(() => {
  invalidateApiCaches();
});
