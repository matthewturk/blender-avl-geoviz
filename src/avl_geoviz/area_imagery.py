"""Fetch imagery for the Area & Features box from public-domain tile
services (geo_tiles) and place it as a GeoTIFF layer or object."""

import os
import queue
import threading

import bpy

from . import _osm_safety_shared, geo_raster, geo_tiles, osm_area
from .country_wireframes import _AVLGISPanel


class GeoImagerySettings(bpy.types.PropertyGroup):
    source: bpy.props.EnumProperty(
        name="Source",
        description="Public-domain or NASA imagery to fetch",
        items=[(s.key, s.label, s.description) for s in geo_tiles.SOURCES],
        default="LANDSAT_2000",
    )
    resolution: bpy.props.FloatProperty(
        name="Resolution",
        description=(
            "Ground size of a pixel to aim for. The source's next finer level is used, "
            "down to its finest; 0 asks for the finest"
        ),
        default=30.0,
        min=0.0,
        soft_max=5000.0,
        unit="LENGTH",
    )
    date: bpy.props.StringProperty(
        name="Date",
        description="Day to fetch for daily sources, as YYYY-MM-DD. Empty is the latest",
        default="",
    )
    patch_size: bpy.props.IntProperty(
        name="Patch Size",
        description=(
            "Largest image to make, in pixels a side. Bigger requests are split into a "
            "grid of patches, each its own object or layer"
        ),
        default=4096,
        min=512,
        max=geo_tiles.MAX_SIDE,
    )


def _settings(context):
    return context.scene.geo_imagery_settings


def current_split(context):
    """(source, Split) for the panel and operator; raises RuntimeError (or
    TileError) if the area or source can't be used."""
    settings = _settings(context)
    source = geo_tiles.SOURCES_BY_KEY[settings.source]
    bbox = osm_area.parse_bbox(osm_area.get(context))
    lat = (bbox[0] + bbox[2]) / 2.0
    level = geo_tiles.level_for(source, settings.resolution, lat)
    return source, geo_tiles.split(source, bbox, level, settings.patch_size)


def _patch_collection(obj, name):
    """Move *obj* into a collection *name* inside the one it's in, to keep
    a request's patches together."""
    parent = obj.users_collection[0]
    coll = parent.children.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        parent.children.link(coll)
    if coll not in obj.users_collection:
        coll.objects.link(obj)
    for other in list(obj.users_collection):
        if other is not coll:
            other.objects.unlink(obj)
    return coll


# Requests with more tiles than this ask before fetching.
CONFIRM_TILES = 200

# The fetch running in the background, if any; one at a time.
_active = None


def active_fetch():
    return _active


class FetchRun:
    """One Area Imagery request. Downloading (and stitching, warping and
    reading back each patch) happens in a worker thread, or inline; placing
    the patches in the scene happens on the main thread, in drain()."""

    def __init__(self, context, source, split, date, target):
        whole = split.whole
        self.source, self.split, self.date, self.target = source, split, date, target
        self.when = geo_tiles.resolve_time(source, date)
        self.base = geo_tiles.request_name(source, whole.bbox, whole.level, self.when)
        self.projection_bounds = (whole.bbox[1], whole.bbox[0], whole.bbox[3], whole.bbox[2])
        # By name: the user can change the selection or delete objects while
        # a background fetch runs.
        self.target_names = ([o.name for o in geo_raster.default_targets(context)]
                             if target == "LAYER" else [])
        self.cache = geo_tiles.cache_dir()
        self.total_tiles = sum(p.plan.tiles for p in split.patches)
        self.done_tiles = 0
        self.patches_done = 0
        self.cancel = threading.Event()
        self.results = queue.Queue()
        self.thread = None
        self.ended = False
        self.error = None
        self.made, self.empty, self.missing, self.problems = [], 0, 0, False
        self.message = None
        self.collection = None

    # -- worker side: no bpy -------------------------------------------------

    def _work(self):
        try:
            for patch in self.split.patches:
                if self.cancel.is_set():
                    break
                offset = self.done_tiles
                name = (self.base if self.split.count == 1
                        else f"{self.base} {patch.row + 1}-{patch.col + 1}")

                def progress(done, _total, offset=offset):
                    self.done_tiles = offset + done

                try:
                    mosaic = geo_tiles.fetch(
                        self.source, patch.plan.bbox, self.split.whole.level, self.date,
                        cache_root=self.cache, progress=progress, cancel=self.cancel)
                except geo_tiles.NoImagery:
                    self.done_tiles = offset + patch.plan.tiles
                    self.results.put(("empty", None))
                    continue
                path = os.path.join(self.cache, name.replace(" ", "_") + ".tif")
                geo_tiles.write_geotiff(path, mosaic)
                raster = geo_raster.read_geotiff(path, max_size=geo_tiles.MAX_SIDE)
                self.results.put(("patch", (patch, name, path, raster, mosaic)))
        except geo_tiles.Cancelled:
            pass
        except Exception as exc:
            self.error = exc
        finally:
            self.results.put(("end", None))

    def start(self, threaded):
        print(f"[GeoViz] Fetching {self.split.tiles} {self.source.short} tile(s) at level "
              f"{self.split.whole.level} as {self.split.nx}x{self.split.ny} patch(es)...")
        if threaded:
            self.thread = threading.Thread(target=self._work, name="GeoViz imagery", daemon=True)
            self.thread.start()
        else:
            self._work()

    # -- main thread ---------------------------------------------------------

    def drain(self, context):
        """Place the patches that are ready; True once the worker is done."""
        while not self.ended:
            try:
                kind, item = self.results.get_nowait()
            except queue.Empty:
                break
            if kind == "end":
                self.ended = True
            elif kind == "empty":
                self.empty += 1
                self.patches_done += 1
            else:
                try:
                    self._place(context, *item)
                except Exception as exc:
                    self.error = exc
                    self.cancel.set()
                self.patches_done += 1
        return self.ended

    def _place(self, context, patch, name, path, raster, mosaic):
        source = self.source
        self.missing += mosaic.missing
        if self.target == "OBJECT":
            image, obj, self.message = geo_raster.raster_object(
                context, name, path, raster,
                extent=patch.extent, projection_bounds=self.projection_bounds)
            obj["geo_attribution"] = source.attribution
            if self.split.count > 1:
                self.collection = _patch_collection(obj, self.base)
            self.made.append(obj.name)
        else:
            targets = [o for o in map(bpy.data.objects.get, self.target_names) if o is not None]
            if not targets:
                raise RuntimeError("the objects to add layers to are gone")
            image, self.message, issues = geo_raster.drape_raster(context, name, path, raster, targets)
            self.problems |= issues
            self.made.append(image.name)
        image["geo_attribution"] = source.attribution
        image["geo_source"] = source.label
        image["geo_level"] = self.split.whole.level
        if self.when:
            image["geo_time"] = self.when

    def status(self):
        # Tiles on patch edges count once per patch (the cache downloads
        # them once), so this is a share of the work, not a tile count.
        percent = 100 * self.done_tiles // max(1, self.total_tiles)
        patches = f", {self.patches_done}/{self.split.count} patches" if self.split.count > 1 else ""
        return f"Fetching {self.source.short} imagery: {percent}%{patches}"

    def finish(self, context, operator):
        """Select what was made and report; returns the operator result."""
        source, split = self.source, self.split
        if self.target == "OBJECT" and self.made:
            objs = [o for o in map(bpy.data.objects.get, self.made) if o is not None]
            for other in context.selected_objects:
                other.select_set(False)
            for obj in objs:
                obj.select_set(True)
            if objs:
                context.view_layer.objects.active = objs[-1]
        message, problems = self.message, self.problems
        if split.count > 1 and self.made:
            kind = ("objects in collection " + self.collection.name
                    if self.target == "OBJECT" else "layers")
            message = (f"Made {len(self.made)} of {split.count} patches ({split.nx}x{split.ny}, "
                       f"{split.whole.size[0]}x{split.whole.size[1]} px in all) as {kind}")
        if self.empty:
            problems = True
            message = (message + "; " if message else "") + (
                f"{self.empty} patch(es) had no imagery and were skipped")
        if self.error is not None:
            operator.report({"ERROR"}, f"Couldn't fetch {source.short} imagery"
                            + (f" (kept {len(self.made)} patch(es))" if self.made else "")
                            + f": {self.error}")
            return {"FINISHED"} if self.made else {"CANCELLED"}
        if self.cancel.is_set():
            operator.report({"WARNING"}, f"Cancelled; kept {len(self.made)} of {split.count} patch(es)")
            return {"FINISHED"} if self.made else {"CANCELLED"}
        if not self.made:
            operator.report({"ERROR"}, f"{source.short} has no imagery here. {source.coverage}")
            return {"CANCELLED"}
        message += f". {source.attribution}"
        if self.missing:
            problems = True
            message += (f". {self.missing} of {split.tiles} tiles have no imagery and are "
                        f"transparent. {source.coverage}")
        operator.report({"WARNING"} if problems else {"INFO"}, message)
        return {"FINISHED"}


def _redraw(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


class OBJECT_OT_fetch_area_imagery(bpy.types.Operator):
    bl_idname = "object.fetch_area_imagery"
    bl_label = "Fetch Area Imagery"
    bl_description = (
        "Download imagery for the Area & Features box from the chosen public-domain "
        "source and place it on the globe or flat map, split into patches if it's "
        "larger than Patch Size. Runs in the background; Esc cancels"
    )
    bl_options = {"REGISTER", "UNDO"}

    target: bpy.props.EnumProperty(
        name="Target",
        items=[
            ("OBJECT", "New Object", "Make objects covering the area with the imagery as their layer"),
            ("LAYER", "Layer on Selected",
             "Add the imagery as layers on the selected objects (the reference globe if none)"),
        ],
        default="OBJECT",
    )
    background: bpy.props.BoolProperty(
        name="In Background",
        description="Fetch without blocking Blender, cancellable with Esc (set when run from the UI)",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context):
        if _active is not None:
            cls.poll_message_set("An imagery fetch is already running")
            return False
        return True

    def _check(self, context):
        """(source, split) or None after reporting why not."""
        if not geo_tiles.online_access():
            self.report({"ERROR"}, "Online access is off: turn on Allow Online Access in "
                                   "Preferences > System")
            return None
        try:
            source, split = current_split(context)
            problem = split.problem()
            if problem:
                raise geo_tiles.TileError(problem)
            geo_tiles.resolve_time(source, _settings(context).date)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return None
        return source, split

    def invoke(self, context, event):
        checked = self._check(context)
        if checked is None:
            return {"CANCELLED"}
        source, split = checked
        self.background = not bpy.app.background
        if split.tiles > CONFIRM_TILES:
            w, h = split.whole.size
            return context.window_manager.invoke_confirm(
                self, event, title="Fetch Area Imagery",
                message=(f"{split.tiles} {source.short} tiles, {w}x{h} px"
                         + (f" in {split.count} patches" if split.count > 1 else "")
                         + ". Esc cancels while it runs."),
                confirm_text="Fetch", icon="QUESTION")
        return self.execute(context)

    def execute(self, context):
        global _active
        checked = self._check(context)
        if checked is None:
            return {"CANCELLED"}
        source, split = checked
        try:
            run = FetchRun(context, source, split, _settings(context).date, self.target)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        if not (self.background and context.window is not None):
            run.start(threaded=False)
            run.drain(context)
            return run.finish(context, self)

        _active = self._run = run
        run.start(threaded=True)
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.2, window=context.window)
        wm.progress_begin(0, max(1, run.total_tiles))
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        run = self._run
        if event.type == "ESC" and event.value == "PRESS":
            run.cancel.set()
            context.workspace.status_text_set("Cancelling imagery fetch...")
            return {"RUNNING_MODAL"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        ended = run.drain(context)
        context.window_manager.progress_update(run.done_tiles)
        if not ended:
            if not run.cancel.is_set():
                context.workspace.status_text_set(run.status() + " (Esc to cancel)")
            _redraw(context)
            return {"PASS_THROUGH"}
        return self._end(context)

    def _end(self, context):
        global _active
        wm = context.window_manager
        wm.event_timer_remove(self._timer)
        wm.progress_end()
        context.workspace.status_text_set(None)
        _active = None
        _redraw(context)
        return self._run.finish(context, self)

    def cancel(self, context):
        # Blender is closing the operator (e.g. the file is being closed).
        global _active
        self._run.cancel.set()
        context.window_manager.event_timer_remove(self._timer)
        context.window_manager.progress_end()
        _active = None


class OBJECT_OT_cancel_area_imagery(bpy.types.Operator):
    bl_idname = "object.cancel_area_imagery"
    bl_label = "Cancel Imagery Fetch"
    bl_description = "Stop the running imagery fetch, keeping the patches already made"
    bl_options = {"INTERNAL"}

    @classmethod
    def poll(cls, context):
        return _active is not None and not _active.cancel.is_set()

    def execute(self, context):
        _active.cancel.set()
        return {"FINISHED"}


class VIEW3D_PT_area_imagery(_AVLGISPanel, bpy.types.Panel):
    bl_label = "Area Imagery"
    bl_idname = "VIEW3D_PT_area_imagery"
    bl_parent_id = "VIEW3D_PT_country_wireframes_imagery"
    bl_order = -2

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        source = geo_tiles.SOURCES_BY_KEY[settings.source]

        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(settings, "source")
        col.prop(settings, "resolution")
        if source.time == "daily":
            col.prop(settings, "date", placeholder="Latest")
        col.prop(settings, "patch_size")

        box = layout.box().column(align=True)
        osm_area.draw_wrapped(box, source.coverage, icon="WORLD")
        osm_area.draw_wrapped(box, source.attribution, icon="COPY_ID")

        col = layout.column(align=True)
        try:
            _source, split = current_split(context)
        except RuntimeError as exc:
            col.alert = True
            osm_area.draw_wrapped(col, f"Area & Features: {exc}", icon="ERROR")
            return
        whole = split.whole
        col.label(text=f"Level {whole.level}: ~{whole.resolution_m:.3g} m/px", icon="IMAGE_DATA")
        problem = split.problem()
        sub = col.column(align=True)
        sub.alert = problem is not None
        sub.label(text=f"{split.tiles} tiles, {whole.size[0]}x{whole.size[1]} px")
        if split.count > 1:
            sub.label(text=f"Split into {split.nx}x{split.ny} = {split.count} patches")
        if problem:
            osm_area.draw_wrapped(sub, problem, icon="ERROR")
        box = layout.box().column(align=True)
        box.label(text="Covers the Area & Features box:", icon="INFO")
        box.label(text=_osm_safety_shared.format_dims(*whole.bbox), icon="BLANK1")
        from .area_tools import draw_area_tools

        draw_area_tools(box, context)

        run = active_fetch()
        if run is not None:
            box = layout.box().column(align=True)
            osm_area.draw_wrapped(box, run.status(), icon="SORTTIME")
            row = box.row()
            row.scale_y = 1.3
            row.operator("object.cancel_area_imagery", icon="CANCEL")
            return
        if split.tiles > CONFIRM_TILES and problem is None:
            layout.label(text="Large request: asks before fetching", icon="QUESTION")

        col = layout.column(align=True)
        col.enabled = problem is None
        col.scale_y = 1.3
        col.operator("object.fetch_area_imagery", text="Fetch as Object", icon="MESH_GRID").target = "OBJECT"
        col.operator("object.fetch_area_imagery", text="Fetch as Layer on Selected",
                     icon="IMAGE_RGB_ALPHA").target = "LAYER"


CLASSES = (
    GeoImagerySettings,
    OBJECT_OT_fetch_area_imagery,
    OBJECT_OT_cancel_area_imagery,
    VIEW3D_PT_area_imagery,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.geo_imagery_settings = bpy.props.PointerProperty(type=GeoImagerySettings)


def unregister():
    del bpy.types.Scene.geo_imagery_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
