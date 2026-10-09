import type { ReactiveController, ReactiveControllerHost } from 'lit';
import { getUserProfile, hasPermission } from '../api';
import type { UserPermissions } from '../permissions';

/** Cached profile permissions with an explicit unknown state for edit controls. */
export class EditPermissions implements ReactiveController {
  private loaded = false;
  private permissions: UserPermissions;
  private generation = 0;

  constructor(private readonly host: ReactiveControllerHost) {
    host.addController(this);
  }

  hostConnected(): void {
    const generation = ++this.generation;
    this.loaded = false;
    void getUserProfile()
      .then((profile) => {
        if (generation !== this.generation) return;
        this.loaded = !!profile;
        this.permissions = profile?.permissions;
        this.host.requestUpdate();
      })
      .catch(() => {
        if (generation !== this.generation) return;
        this.loaded = false;
        this.host.requestUpdate();
      });
  }

  hostDisconnected(): void {
    ++this.generation;
    this.loaded = false;
  }

  allows(permission: string): boolean {
    return this.loaded && hasPermission(this.permissions, permission);
  }
}
