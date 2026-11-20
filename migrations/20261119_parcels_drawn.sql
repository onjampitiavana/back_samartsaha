-- ============================================================================
-- Smart Saha - 4.2 parcel drawn on the web map (manager)
-- Run once in the Supabase SQL editor, after 20261116_performance.sql.
--
-- insert_drawn_parcel(coop, farmer, geojson, nom) stores ONE polygon drawn on
-- the map, with exactly the rules of the file import (insert_parcels):
--   * the cooperative and the farmer belong to the caller, the farmer to that
--     cooperative (same checks, same messages);
--   * ST_GeomFromGeoJSON -> ST_SetSRID 4326 -> ST_Force2D, ST_MakeValid only
--     when ST_IsValid fails, then POLYGON / MULTIPOLYGON only;
--   * area_ha = round(ST_Area(geom::geography) / 10000, 4)  (geodesic, WGS 84).
-- Unlike insert_parcels it returns the stored row (uuid, nom, area_ha, kind)
-- so the page can show the area PostGIS saved.
-- Idempotent: can be run again without error.
-- ============================================================================

create or replace function public.insert_drawn_parcel(
  coop_id      uuid,
  farmer_id_in uuid,
  geom_geojson jsonb,
  nom_in       text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  g       geometry;
  new_id  uuid;
  saved   record;
begin
  if auth.uid() is null then
    raise exception 'Not authenticated';
  end if;
  if not exists (select 1 from cooperative where uuid = coop_id and owner = auth.uid()) then
    raise exception 'Cooperative not found';
  end if;
  if not exists (select 1 from farmers
                 where uuid = farmer_id_in and owner = auth.uid() and cooperative_id = coop_id) then
    raise exception 'Farmer not found';
  end if;
  if geom_geojson is null or geom_geojson ->> 'type' not in ('Polygon', 'MultiPolygon') then
    raise exception 'Shape 1 is not a polygon';
  end if;

  -- Same normalisation as insert_parcels / insert_parcel.
  g := ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(geom_geojson::text), 4326));
  if not ST_IsValid(g) then
    g := ST_CollectionExtract(ST_MakeValid(g), 3);   -- polygons only
  end if;
  if g is null or ST_IsEmpty(g) or GeometryType(g) not in ('POLYGON', 'MULTIPOLYGON') then
    raise exception 'Shape 1 is not a polygon';
  end if;

  insert into parcels (cooperative_id, farmer_id, owner, nom, geom, area_ha)
  values (coop_id, farmer_id_in, auth.uid(), left(btrim(nom_in), 120), g,
          round((ST_Area(g::geography) / 10000)::numeric, 4))
  returning uuid into new_id;

  select p.uuid, p.nom, p.area_ha, GeometryType(p.geom) as kind
  into saved
  from parcels p where p.uuid = new_id;

  return jsonb_build_object('uuid', saved.uuid, 'nom', saved.nom,
                            'area_ha', saved.area_ha, 'kind', saved.kind);
end;
$$;

revoke execute on function public.insert_drawn_parcel(uuid, uuid, jsonb, text) from public, anon;
grant execute on function public.insert_drawn_parcel(uuid, uuid, jsonb, text) to authenticated;

notify pgrst, 'reload schema';
