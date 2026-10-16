-- Native, read-only S3-compatible inventory for the first-party staff console.
-- The platform's administrator role may browse bucket metadata; it never gains
-- direct storage credentials or access to object bytes from this permission.
INSERT INTO public.permissions (code, resource, action, description)
    VALUES ('storage.manage', 'storage', 'manage', 'Browse private object-storage inventory')
    ON CONFLICT (code) DO NOTHING;

-- Existing installs receive the narrowly-scoped capability on their protected
-- system administrator role. New installs get it from the seeded catalog.
INSERT INTO public.role_permissions (role_id, permission_code)
    SELECT id, 'storage.manage'
    FROM public.roles
    WHERE name = 'administrator' AND is_system
    ON CONFLICT DO NOTHING;
