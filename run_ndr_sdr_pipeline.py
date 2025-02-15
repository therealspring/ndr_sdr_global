"""Run SDR and NDR models on large spatial footprint."""
from datetime import datetime
import argparse
import collections
import configparser
import glob
import gzip
import itertools
import logging
import multiprocessing
import os
import shutil
import sys
import threading
import time

from inspring import sdr_c_factor
from inspring import ndr_mfd_plus
from ecoshard import geoprocessing
from ecoshard import taskgraph
from osgeo import gdal
from osgeo import ogr
from osgeo import osr
import ecoshard
import requests


gdal.SetCacheMax(2**26)
logging.basicConfig(
    level=logging.DEBUG,
    format=(
        '%(asctime)s (%(relativeCreated)d) %(levelname)s %(name)s'
        ' [%(funcName)s:%(lineno)d] %(message)s'),
    stream=sys.stdout)
logging.getLogger('ecoshard.taskgraph').setLevel(logging.INFO)
logging.getLogger('ecoshard.ecoshard').setLevel(logging.INFO)
logging.getLogger('urllib3.connectionpool').setLevel(logging.INFO)
logging.getLogger('ecoshard.geoprocessing.geoprocessing').setLevel(
    logging.ERROR)
logging.getLogger('ecoshard.geoprocessing.routing.routing').setLevel(
    logging.WARNING)
logging.getLogger('ecoshard.geoprocessing.geoprocessing_core').setLevel(
    logging.ERROR)
logging.getLogger('inspring.sdr_c_factor').setLevel(logging.WARNING)
logging.getLogger('inspring.ndr_mfd_plus').setLevel(logging.WARNING)

LOGGER = logging.getLogger(__name__)

N_TO_BUFFER_STITCH = 10


def _parse_non_default_options(config, section):
    return set([
        x for x in config[section]
        if x not in config._defaults])


def _flatten_dir(working_dir):
    """Move all files in subdirectory to `working_dir`."""
    all_files = []
    # itertools lets us skip the first iteration (current dir)
    for root, _dirs, files in itertools.islice(os.walk(working_dir), 1, None):
        for filename in files:
            all_files.append(os.path.join(root, filename))
    for filename in all_files:
        shutil.move(filename, working_dir)


def _unpack_and_vrt_tiles(
        zip_path, unpack_dir, target_nodata, target_vrt_path):
    """Unzip multi-file of tiles and create VRT.

    Args:
        zip_path (str): path to zip file of tiles
        unpack_dir (str): path to directory to unpack tiles
        target_vrt_path (str): desired target path for VRT.

    Returns:
        None
    """
    if not os.path.exists(target_vrt_path):
        shutil.unpack_archive(zip_path, unpack_dir)
        _flatten_dir(unpack_dir)
        base_raster_path_list = glob.glob(os.path.join(unpack_dir, '*.tif'))
        vrt_options = gdal.BuildVRTOptions(VRTNodata=target_nodata)
        gdal.BuildVRT(
            target_vrt_path, base_raster_path_list, options=vrt_options)
        target_dem = gdal.OpenEx(target_vrt_path, gdal.OF_RASTER)
        if target_dem is None:
            raise RuntimeError(
                f"didn't make VRT at {target_vrt_path} on: {zip_path}")


def _download_and_validate(url, target_path):
    """Download an ecoshard and validate its hash."""
    ecoshard.download_url(url, target_path)
    if not ecoshard.validate(target_path):
        raise ValueError(f'{target_path} did not validate on its hash')


def _download_and_set_nodata(url, nodata, target_path):
    """Download and set nodata value if needed."""
    ecoshard.download_url(url, target_path)
    if nodata is not None:
        raster = gdal.OpenEx(target_path, gdal.GA_Update)
        band = raster.GetRasterBand(1)
        band.SetNoDataValue(nodata)
        band = None
        raster = None


def fetch_data(ecoshard_map, data_dir):
    """Download data in `ecoshard_map` and replace urls with targets.

    Any values that are not urls are kept and a warning is logged.

    Args:
        ecoshard_map (dict): key/value pairs where if value is a url that
            file is downloaded and verified against its hash.
        data_dir (str): path to a directory to store downloaded data.

    Returns:
        dict of {value: filepath} map where `filepath` is the path to the
            downloaded file stored in `data_dir`. If the original value was
            not a url it is copied as-is.
    """
    task_graph = taskgraph.TaskGraph(
        data_dir, multiprocessing.cpu_count(), parallel_mode='thread',
        taskgraph_name='fetch data')
    data_map = {}
    for key, value in ecoshard_map.items():
        if value is None:
            continue
        if isinstance(value, tuple):
            url, nodata = value
        else:
            url = value
            nodata = None
        if url.startswith('http'):
            target_path = os.path.join(data_dir, os.path.basename(url))
            data_map[key] = target_path
            if os.path.exists(target_path):
                LOGGER.info(f'{target_path} exists, so skipping download')
                continue
            response = requests.head(url)
            if response:
                target_path = os.path.join(data_dir, os.path.basename(url))
                if not os.path.exists(target_path):
                    task_graph.add_task(
                        func=_download_and_set_nodata,
                        args=(url, nodata, target_path),
                        target_path_list=[target_path],
                        task_name=f'download {url}')
            else:
                raise ValueError(f'{key}: {url} does not refer to a url')
        else:
            if not os.path.exists(url):
                raise ValueError(
                    f'expected an existing file for {key} at {url} but not found')
            data_map[key] = url
    LOGGER.info('waiting for downloads to complete')
    task_graph.close()
    task_graph.join()
    task_graph = None
    LOGGER.debug(data_map)
    return data_map


def _unpack_archive(archive_path, dest_dir):
    """Unpack archive to dest_dir."""
    if archive_path.endswith('.gz'):
        with gzip.open(archive_path, 'r') as f_in:
            dest_path = os.path.join(
                dest_dir, os.path.basename(os.path.splitext(archive_path)[0]))
            with open(dest_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
    else:
        shutil.unpack_archive(archive_path, dest_dir)


def fetch_and_unpack_data(task_graph, config, scenario_id):
    """Fetch & unpack data subroutine."""
    data_dir = os.path.join(config.get(scenario_id, 'WORKSPACE_DIR'), 'data')
    LOGGER.info('downloading data')

    ecoshard_map = {}
    # hard code DEM and WATERSHEDS because it's both default and in 'files'
    for file_key in _parse_non_default_options(config, 'files') | set(['DEM', 'WATERSHEDS']):
        LOGGER.debug(file_key)
        ecoshard_map[file_key.upper()] = config.get(
            scenario_id, file_key, fallback=None)
    LOGGER.debug(ecoshard_map)
    fetch_task = task_graph.add_task(
        func=fetch_data,
        args=(ecoshard_map, data_dir),
        store_result=True,
        transient_run=True,
        task_name='download ecoshards')
    file_map = fetch_task.get()
    LOGGER.info('downloaded data')
    dem_dir = os.path.splitext(file_map['DEM'])[0]
    dem_vrt_path = os.path.join(dem_dir, 'dem.vrt')
    LOGGER.info('unpack dem')
    _ = task_graph.add_task(
        func=_unpack_and_vrt_tiles,
        args=(file_map['DEM'], dem_dir, -9999, dem_vrt_path),
        target_path_list=[dem_vrt_path],
        task_name=f'unpack {file_map["DEM"]}')
    file_map['DEM'] = dem_vrt_path
    LOGGER.debug(file_map)
    LOGGER.info('unpack watersheds')
    _ = task_graph.add_task(
        func=_unpack_archive,
        args=(file_map['WATERSHEDS'], data_dir),
        task_name=f'decompress {file_map["WATERSHEDS"]}')
    file_map['WATERSHEDS'] = data_dir
    task_graph.join()

    # just need the base directory for watersheds
    file_map['WATERSHEDS'] = os.path.join(
        file_map['WATERSHEDS'], 'watersheds_globe_HydroSHEDS_15arcseconds')

    return file_map


def _batch_into_watershed_subsets(
        watershed_root_dir, degree_separation, done_token_path,
        global_bb, min_watershed_area, watershed_subset=None):
    """Construct geospatially adjacent subsets.

    Breaks watersheds up into geospatially similar watersheds and limits
    the upper size to no more than specified area. This allows for a
    computationally efficient batch to run on a large contiguous area in
    parallel while avoiding batching watersheds that are too small.

    Args:
        watershed_root_dir (str): path to watershed .shp files.
        degree_separation (int): a blocksize number of degrees to coalasce
            watershed subsets into.
        done_token_path (str): path to file to write when function is
            complete, indicates for batching that the task is complete.
        global_bb (list): min_lng, min_lat, max_lng, max_lat bounding box
            to limit watersheds to be selected from
        min_watershed_area (float): mininmum size of a watershed in square
            degrees to avoid processing
        watershed_subset (dict): if not None, keys are watershed basefile
            names and values are FIDs to select. If present the simulation
            only constructs batches from these watershed/fids, otherwise
            all watersheds are run.

    Returns:
        list of (job_id, watershed.gpkg) tuples where the job_id is a
        unique identifier for that subwatershed set and watershed.gpkg is
        a subset of the original global watershed files.

    """
    # ensures we don't have more than 1000 watersheds per job
    task_graph = taskgraph.TaskGraph(
        watershed_root_dir, multiprocessing.cpu_count(), 10,
        taskgraph_name='batch watersheds')
    watershed_path_area_list = []
    job_id_set = set()
    for watershed_path in glob.glob(
            os.path.join(watershed_root_dir, '*.shp')):
        LOGGER.debug(f'scheduling {os.path.basename(watershed_path)}')
        subbatch_job_index_map = collections.defaultdict(int)
        # lambda describes the FIDs to process per job, the list of lat/lng
        # bounding boxes for each FID, and the total degree area of the job
        watershed_fid_index = collections.defaultdict(
            lambda: [list(), list(), 0])
        watershed_basename = os.path.splitext(
            os.path.basename(watershed_path))[0]
        watershed_ids = None
        watershed_vector = gdal.OpenEx(watershed_path, gdal.OF_VECTOR)
        watershed_layer = watershed_vector.GetLayer()

        if watershed_subset:
            if watershed_basename not in watershed_subset:
                continue
            else:
                # just grab the subset
                watershed_ids = watershed_subset[watershed_basename]
                watershed_layer = [
                    watershed_layer.GetFeature(fid) for fid in watershed_ids]

        # watershed layer is either the layer or a list of features
        for watershed_feature in watershed_layer:
            fid = watershed_feature.GetFID()
            watershed_geom = watershed_feature.GetGeometryRef()
            watershed_centroid = watershed_geom.Centroid()
            epsg = geoprocessing.get_utm_zone(
                watershed_centroid.GetX(), watershed_centroid.GetY())
            if watershed_geom.Area() > 1 or watershed_ids:
                # one degree grids or immediates get special treatment
                job_id = (f'{watershed_basename}_{fid}', epsg)
                watershed_fid_index[job_id][0] = [fid]
            else:
                # clamp into degree_separation squares
                x, y = [
                    int(v//degree_separation)*degree_separation for v in (
                        watershed_centroid.GetX(), watershed_centroid.GetY())]
                base_job_id = f'{watershed_basename}_{x}_{y}'
                # keep the epsg in the string because the centroid might lie
                # on a different boundary
                job_id = (f'''{base_job_id}_{
                    subbatch_job_index_map[base_job_id]}_{epsg}''', epsg)
                if len(watershed_fid_index[job_id][0]) > 1000:
                    subbatch_job_index_map[base_job_id] += 1
                    job_id = (f'''{base_job_id}_{
                        subbatch_job_index_map[base_job_id]}_{epsg}''', epsg)
                watershed_fid_index[job_id][0].append(fid)
            watershed_envelope = watershed_geom.GetEnvelope()
            watershed_bb = [watershed_envelope[i] for i in [0, 2, 1, 3]]
            if global_bb is not None and (watershed_bb[0] < global_bb[0] or
                                          watershed_bb[2] > global_bb[2] or
                                          watershed_bb[1] > global_bb[3] or
                                          watershed_bb[3] < global_bb[1]):
                LOGGER.warning(
                    f'{watershed_bb} is on a dangerous boundary so dropping')
                watershed_fid_index[job_id][0].pop()
                continue
            watershed_fid_index[job_id][1].append(watershed_bb)
            watershed_fid_index[job_id][2] += watershed_geom.Area()

        watershed_geom = None
        watershed_feature = None

        watershed_subset_dir = os.path.join(
            watershed_root_dir, 'watershed_subsets')
        os.makedirs(watershed_subset_dir, exist_ok=True)

        for (job_id, epsg), (fid_list, watershed_envelope_list, area) in \
                sorted(
                    watershed_fid_index.items(), key=lambda x: x[1][-1],
                    reverse=True):
            if job_id in job_id_set:
                raise ValueError(f'{job_id} already processed')
            if len(watershed_envelope_list) < 3 and area < min_watershed_area:
                # it's too small to process
                continue
            job_id_set.add(job_id)

            watershed_subset_path = os.path.join(
                watershed_subset_dir, f'{job_id}_a{area:.3f}.gpkg')
            if not os.path.exists(watershed_subset_path):
                task_graph.add_task(
                    func=_create_fid_subset,
                    args=(
                        watershed_path, fid_list, epsg, watershed_subset_path),
                    target_path_list=[watershed_subset_path],
                    task_name=job_id)
            watershed_path_area_list.append(
                (area, watershed_subset_path))

        watershed_layer = None
        watershed_vector = None

    task_graph.join()
    task_graph.close()
    task_graph = None

    # create a global sorted watershed path list so it's sorted by area overall
    # not just by region per area
    with open(done_token_path, 'w') as token_file:
        token_file.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    sorted_watershed_path_list = [
        path for area, path in sorted(watershed_path_area_list, reverse=True)]
    return sorted_watershed_path_list


def _create_fid_subset(
        base_vector_path, fid_list, target_epsg, target_vector_path):
    """Create subset of vector that matches fid list, projected into epsg."""
    vector = gdal.OpenEx(base_vector_path, gdal.OF_VECTOR)
    layer = vector.GetLayer()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(target_epsg)
    layer.SetAttributeFilter(
        f'"FID" in ('
        f'{", ".join([str(v) for v in fid_list])})')
    feature_count = layer.GetFeatureCount()
    gpkg_driver = ogr.GetDriverByName('gpkg')
    unprojected_vector_path = '%s_wgs84%s' % os.path.splitext(
        target_vector_path)
    subset_vector = gpkg_driver.CreateDataSource(unprojected_vector_path)
    subset_vector.CopyLayer(
        layer, os.path.basename(os.path.splitext(target_vector_path)[0]))
    geoprocessing.reproject_vector(
        unprojected_vector_path, srs.ExportToWkt(), target_vector_path,
        driver_name='gpkg', copy_fields=False)
    subset_vector = None
    layer = None
    vector = None
    gpkg_driver.DeleteDataSource(unprojected_vector_path)
    target_vector = gdal.OpenEx(target_vector_path, gdal.OF_VECTOR)
    target_layer = target_vector.GetLayer()
    if feature_count != target_layer.GetFeatureCount():
        raise ValueError(
            f'expected {feature_count} in {target_vector_path} but got '
            f'{target_layer.GetFeatureCount()}')


def _run_sdr(
        task_graph,
        workspace_dir,
        watershed_path_list,
        dem_path,
        erosivity_path,
        erodibility_path,
        lulc_path,
        usle_c_path,
        usle_p_path,
        target_pixel_size,
        biophysical_table_path,
        biophysical_table_lucode_field,
        threshold_flow_accumulation,
        l_cap,
        k_param,
        sdr_max,
        ic_0_param,
        target_stitch_raster_map,
        global_pixel_size_deg,
        keep_intermediate_files=False,
        c_factor_path=None,
        result_suffix=None,
        ):
    """Run SDR component of the pipeline.

    This function will iterate through the watershed subset list, run the SDR
    model on those subwatershed regions, and stitch those data back into a
    global raster.

    Args:
        workspace_dir (str): path to directory to do all work
        watershed_path_list (list): list of watershed vector files to
            operate on locally. The base filenames are used as the workspace
            directory path.
        dem_path (str): path to global DEM raster
        erosivity_path (str): path to global erosivity raster
        erodibility_path (str): path to erodability raster
        lulc_path (str): path to landcover raster
        usle_c_path (str): optional path to continuous C factor raster
        usle_p_path (str): optional path to continuous P factor raster
        target_pixel_size (float): target projected pixel unit size
        biophysical_table_lucode_field (str): name of the lucode field in
            the biophysical table column
        threshold_flow_accumulation (float): flow accumulation threshold
            to use to calculate streams.
        l_cap (float): upper limit to the L factor
        k_param (float): k parameter in SDR model
        sdr_max (float): max SDR value
        ic_0_param (float): IC0 constant in SDR model
        target_stitch_raster_map (dict): maps the local path of an output
            raster of this model to an existing global raster to stich into.
        keep_intermediate_files (bool): if True, the intermediate watershed
            workspace created underneath `workspace_dir` is deleted.
        c_factor_path (str): optional, path to c factor that's used for lucodes
            that use the raster
        result_suffix (str): optional, prepended to the global stitch results.

    Returns:
        None.
    """
    # create intersecting bounding box of input data
    global_wgs84_bb = _calculate_intersecting_bounding_box(
        [dem_path, erosivity_path, erodibility_path, lulc_path,
         usle_c_path, usle_p_path])

    # create global stitch rasters and start workers
    stitch_raster_queue_map = {}
    stitch_worker_list = []
    multiprocessing_manager = multiprocessing.Manager()
    signal_done_queue = multiprocessing_manager.Queue()
    for local_result_path, global_stitch_raster_path in \
            target_stitch_raster_map.items():
        if result_suffix is not None:
            global_stitch_raster_path = (
                f'%s_{result_suffix}%s' % os.path.splitext(
                    global_stitch_raster_path))
            local_result_path = (
                f'%s_{result_suffix}%s' % os.path.splitext(
                    local_result_path))
        if not os.path.exists(global_stitch_raster_path):
            LOGGER.info(f'creating {global_stitch_raster_path}')
            driver = gdal.GetDriverByName('GTiff')
            n_cols = int((global_wgs84_bb[2]-global_wgs84_bb[0])/global_pixel_size_deg)
            n_rows = int((global_wgs84_bb[3]-global_wgs84_bb[1])/global_pixel_size_deg)
            LOGGER.info(f'**** creating raster of size {n_cols} by {n_rows}')
            target_raster = driver.Create(
                global_stitch_raster_path,
                n_cols, n_rows, 1,
                gdal.GDT_Float32,
                options=(
                    'TILED=YES', 'BIGTIFF=YES', 'COMPRESS=LZW', 'PREDICTOR=2',
                    'SPARSE_OK=TRUE', 'BLOCKXSIZE=256', 'BLOCKYSIZE=256'))
            wgs84_srs = osr.SpatialReference()
            wgs84_srs.ImportFromEPSG(4326)
            target_raster.SetProjection(wgs84_srs.ExportToWkt())
            target_raster.SetGeoTransform(
                [global_wgs84_bb[0], global_pixel_size_deg, 0,
                 global_wgs84_bb[3], 0, -global_pixel_size_deg])
            target_band = target_raster.GetRasterBand(1)
            target_band.SetNoDataValue(-9999)
            target_raster = None
        stitch_queue = multiprocessing_manager.Queue(N_TO_BUFFER_STITCH*2)
        stitch_thread = threading.Thread(
            target=stitch_worker,
            args=(
                stitch_queue, global_stitch_raster_path,
                len(watershed_path_list),
                signal_done_queue))
        stitch_thread.start()
        stitch_raster_queue_map[local_result_path] = stitch_queue
        stitch_worker_list.append(stitch_thread)

    clean_workspace_worker = threading.Thread(
        target=_clean_workspace_worker,
        args=(len(target_stitch_raster_map), signal_done_queue,
              keep_intermediate_files))
    clean_workspace_worker.daemon = True
    clean_workspace_worker.start()

    # Iterate through each watershed subset and run SDR
    # stitch the results of whatever outputs to whatever global output raster.
    for index, watershed_path in enumerate(watershed_path_list):
        local_workspace_dir = os.path.join(
            workspace_dir, os.path.splitext(
                os.path.basename(watershed_path))[0])
        task_name = f'sdr {os.path.basename(local_workspace_dir)}'
        task_graph.add_task(
            func=_execute_sdr_job,
            args=(
                global_wgs84_bb, watershed_path, local_workspace_dir,
                dem_path, erosivity_path, erodibility_path, lulc_path,
                biophysical_table_path, usle_c_path, usle_p_path,
                threshold_flow_accumulation, k_param,
                sdr_max, ic_0_param, l_cap, target_pixel_size,
                biophysical_table_lucode_field, stitch_raster_queue_map,
                result_suffix),
            transient_run=False,
            priority=-index,  # priority in insert order
            task_name=task_name)

    LOGGER.info('wait for SDR jobs to complete')
    task_graph.join()
    for local_result_path, stitch_queue in stitch_raster_queue_map.items():
        stitch_queue.put(None)
    LOGGER.info('all done with SDR, waiting for stitcher to terminate')
    for stitch_thread in stitch_worker_list:
        stitch_thread.join()
    LOGGER.info(
        'all done with stitching, waiting for workspace worker to terminate')
    signal_done_queue.put(None)
    clean_workspace_worker.join()

    LOGGER.info('all done with SDR -- stitcher terminated')


def _execute_sdr_job(
        global_wgs84_bb, watersheds_path, local_workspace_dir, dem_path,
        erosivity_path, erodibility_path, lulc_path, biophysical_table_path,
        usle_c_path, usle_p_path,
        threshold_flow_accumulation, k_param, sdr_max, ic_0_param, l_cap,
        target_pixel_size, biophysical_table_lucode_field,
        stitch_raster_queue_map, result_suffix):
    """Worker to execute sdr and send signals to stitcher.

    Args:
        global_wgs84_bb (list): bounding box to limit run to, if watersheds do
            not fit, then skip
        watersheds_path (str): path to watershed to run model over
        local_workspace_dir (str): path to local directory

        SDR arguments:
            dem_path
            erosivity_path
            erodibility_path
            lulc_path
            biophysical_table_path
            usle_c_path
            usle_p_path
            threshold_flow_accumulation
            k_param
            sdr_max
            ic_0_param
            l_cap
            target_pixel_size
            biophysical_table_lucode_field
            result_suffix

        stitch_raster_queue_map (dict): map of local result path to
            the stitch queue to signal when job is done.

    Returns:
        None.
    """
    if not _watersheds_intersect(global_wgs84_bb, watersheds_path):
        LOGGER.debug(f'{watersheds_path} does not overlap {global_wgs84_bb}')
        for local_result_path, stitch_queue in stitch_raster_queue_map.items():
            # indicate skipping
            stitch_queue.put((None, 1))

        return

    local_sdr_taskgraph = taskgraph.TaskGraph(local_workspace_dir, -1)
    dem_pixel_size = geoprocessing.get_raster_info(dem_path)['pixel_size']
    base_raster_path_list = [
        dem_path, erosivity_path, erodibility_path, lulc_path]
    resample_method_list = ['bilinear', 'bilinear', 'bilinear', 'mode']

    clipped_data_dir = os.path.join(local_workspace_dir, 'data')
    os.makedirs(clipped_data_dir, exist_ok=True)
    watershed_info = geoprocessing.get_vector_info(watersheds_path)
    target_projection_wkt = watershed_info['projection_wkt']
    watershed_bb = watershed_info['bounding_box']
    lat_lng_bb = geoprocessing.transform_bounding_box(
        watershed_bb, target_projection_wkt, osr.SRS_WKT_WGS84_LAT_LONG)

    warped_raster_path_list = [
        None if path is None else os.path.join(
            clipped_data_dir, os.path.basename(path))
        for path in base_raster_path_list]

    # re-warp stuff we already did
    _warp_raster_stack(
        local_sdr_taskgraph, base_raster_path_list, warped_raster_path_list,
        resample_method_list, dem_pixel_size, target_pixel_size,
        lat_lng_bb, osr.SRS_WKT_WGS84_LAT_LONG, watersheds_path)
    local_sdr_taskgraph.join()

    # clip to lat/lng bounding boxes
    args = {
        'workspace_dir': local_workspace_dir,
        'dem_path': warped_raster_path_list[0],
        'erosivity_path': warped_raster_path_list[1],
        'erodibility_path': warped_raster_path_list[2],
        'lulc_path': warped_raster_path_list[3],
        'prealigned': True,
        'watersheds_path': watersheds_path,
        'biophysical_table_path': biophysical_table_path,
        'threshold_flow_accumulation': threshold_flow_accumulation,
        'usle_c_path': usle_c_path,
        'usle_p_path': usle_p_path,
        'k_param': k_param,
        'sdr_max': sdr_max,
        'ic_0_param': ic_0_param,
        'l_cap': l_cap,
        'results_suffix': result_suffix,
        'biophysical_table_lucode_field': biophysical_table_lucode_field,
        'single_outlet': geoprocessing.get_vector_info(
            watersheds_path)['feature_count'] == 1,
        'prealigned': True,
        'reuse_dem': True,
    }
    sdr_c_factor.execute(args)
    for local_result_path, stitch_queue in stitch_raster_queue_map.items():
        stitch_queue.put(
            (os.path.join(args['workspace_dir'], local_result_path), 1))


def _execute_ndr_job(
        global_wgs84_bb, watersheds_path, local_workspace_dir, dem_path,
        lulc_path,
        runoff_proxy_path, fertilizer_path, biophysical_table_path,
        threshold_flow_accumulation, k_param, target_pixel_size,
        biophysical_table_lucode_field, stitch_raster_queue_map,
        result_suffix):
    """Execute NDR for watershed and push to stitch raster.

        Args:
            global_wgs84_bb (list): global bounding box to test watershed
                overlap with

        args['workspace_dir'] (string):  path to current workspace
        args['dem_path'] (string): path to digital elevation map raster
        args['lulc_path'] (string): a path to landcover map raster
        args['runoff_proxy_path'] (string): a path to a runoff proxy raster
        args['watersheds_path'] (string): path to the watershed shapefile
        args['biophysical_table_path'] (string): path to csv table on disk
            containing nutrient retention values.

            Must contain the following headers:

            'load_n', 'eff_n', 'crit_len_n'

        args['results_suffix'] (string): (optional) a text field to append to
            all output files
        rgs['fertilizer_path'] (string): path to raster to use for fertlizer
            rates when biophysical table uses a 'use raster' value for the
            biophysical table field.
        args['threshold_flow_accumulation']: a number representing the flow
            accumulation in terms of upstream pixels.
        args['k_param'] (number): The Borselli k parameter. This is a
            calibration parameter that determines the shape of the
            relationship between hydrologic connectivity.
        args['target_pixel_size'] (2-tuple): optional, requested target pixel
            size in local projection coordinate system. If not provided the
            pixel size is the smallest of all the input rasters.
        args['target_projection_wkt'] (str): optional, if provided the
            model is run in this target projection. Otherwise runs in the DEM
            projection.
        args['single_outlet'] (str): if True only one drain is modeled, either
            a large sink or the lowest pixel on the edge of the dem.
        result_suffix (str): string to append to NDR files.
    """
    if not _watersheds_intersect(global_wgs84_bb, watersheds_path):
        for local_result_path, stitch_queue in stitch_raster_queue_map.items():
            # indicate skipping
            stitch_queue.put((None, 1))
        return

    local_ndr_taskgraph = taskgraph.TaskGraph(local_workspace_dir, -1)
    dem_pixel_size = geoprocessing.get_raster_info(dem_path)['pixel_size']
    base_raster_path_list = [
        dem_path, runoff_proxy_path, lulc_path, fertilizer_path]
    resample_method_list = ['bilinear', 'bilinear', 'mode', 'bilinear']

    clipped_data_dir = os.path.join(local_workspace_dir, 'data')
    os.makedirs(clipped_data_dir, exist_ok=True)
    watershed_info = geoprocessing.get_vector_info(watersheds_path)
    target_projection_wkt = watershed_info['projection_wkt']
    watershed_bb = watershed_info['bounding_box']
    lat_lng_bb = geoprocessing.transform_bounding_box(
        watershed_bb, target_projection_wkt, osr.SRS_WKT_WGS84_LAT_LONG)
    print(lat_lng_bb)

    warped_raster_path_list = [
        os.path.join(clipped_data_dir, os.path.basename(path))
        for path in base_raster_path_list]

    print(dem_pixel_size)
    print(target_pixel_size)
    sys.exit()
    _warp_raster_stack(
        local_ndr_taskgraph, base_raster_path_list, warped_raster_path_list,
        resample_method_list, dem_pixel_size, target_pixel_size,
        lat_lng_bb, osr.SRS_WKT_WGS84_LAT_LONG, watersheds_path)
    local_ndr_taskgraph.join()

    args = {
        'workspace_dir': local_workspace_dir,
        'dem_path': warped_raster_path_list[0],
        'runoff_proxy_path': warped_raster_path_list[1],
        'lulc_path': warped_raster_path_list[2],
        'fertilizer_path': warped_raster_path_list[3],
        'watersheds_path': watersheds_path,
        'biophysical_table_path': biophysical_table_path,
        'threshold_flow_accumulation': threshold_flow_accumulation,
        'k_param': k_param,
        'target_pixel_size': (target_pixel_size, -target_pixel_size),
        'target_projection_wkt': target_projection_wkt,
        'single_outlet': geoprocessing.get_vector_info(
            watersheds_path)['feature_count'] == 1,
        'biophyisical_lucode_fieldname': biophysical_table_lucode_field,
        'crit_len_n': 150.0,
        'prealigned': True,
        'reuse_dem': True,
        'results_suffix': result_suffix,
    }
    ndr_mfd_plus.execute(args)
    for local_result_path, stitch_queue in stitch_raster_queue_map.items():
        stitch_queue.put(
            (os.path.join(args['workspace_dir'], local_result_path), 1))


def _clean_workspace_worker(
        expected_signal_count, stitch_done_queue, keep_intermediate_files):
    """Removes workspaces when completed.

    Args:
        expected_signal_count (int): the number of times to be notified
            of a done path before it should be deleted.
        stitch_done_queue (queue): will contain directory paths with the
            same directory path appearing `expected_signal_count` times,
            the directory will be removed. Recieving `None` will terminate
            the process.
        keep_intermediate_files (bool): keep intermediate files if true

    Returns:
        None
    """
    try:
        count_dict = collections.defaultdict(int)
        while True:
            dir_path = stitch_done_queue.get()
            if dir_path is None:
                LOGGER.info('recieved None, quitting clean_workspace_worker')
                return
            count_dict[dir_path] += 1
            if count_dict[dir_path] == expected_signal_count:
                LOGGER.info(
                    f'removing {dir_path} after {count_dict[dir_path]} '
                    f'signals')
                if not keep_intermediate_files:
                    shutil.rmtree(dir_path)
                del count_dict[dir_path]
    except Exception:
        LOGGER.exception('error on clean_workspace_worker')


def stitch_worker(
        rasters_to_stitch_queue, target_stitch_raster_path, n_expected,
        signal_done_queue):
    """Update the database with completed work.

    Args:
        rasters_to_stitch_queue (queue): queue that recieves paths to
            rasters to stitch into target_stitch_raster_path.
        target_stitch_raster_path (str): path to an existing raster to stitch
            into.
        n_expected (int): number of expected stitch signals
        signal_done_queue (queue): as each job is complete the directory path
            to the raster will be passed in to eventually remove.


    Return:
        ``None``
    """
    try:
        processed_so_far = 0
        n_buffered = 0
        start_time = time.time()
        stitch_buffer_list = []
        LOGGER.info(f'started stitch worker for {target_stitch_raster_path}')
        while True:
            payload = rasters_to_stitch_queue.get()
            if payload is not None:
                if payload[0] is None:  # means skip this raster
                    processed_so_far += 1
                    continue
                stitch_buffer_list.append(payload)

            if len(stitch_buffer_list) > N_TO_BUFFER_STITCH or payload is None:
                LOGGER.info(
                    f'about to stitch {n_buffered} into '
                    f'{target_stitch_raster_path}')
                geoprocessing.stitch_rasters(
                    stitch_buffer_list, ['near']*len(stitch_buffer_list),
                    (target_stitch_raster_path, 1),
                    area_weight_m2_to_wgs84=True,
                    overlap_algorithm='replace')
                #  _ is the band number
                for stitch_path, _ in stitch_buffer_list:
                    signal_done_queue.put(os.path.dirname(stitch_path))
                stitch_buffer_list = []

            if payload is None:
                LOGGER.info(f'all done sitching {target_stitch_raster_path}')
                return

            processed_so_far += 1
            jobs_per_sec = processed_so_far / (time.time() - start_time)
            remaining_time_s = (
                n_expected / jobs_per_sec)
            remaining_time_h = int(remaining_time_s // 3600)
            remaining_time_s -= remaining_time_h * 3600
            remaining_time_m = int(remaining_time_s // 60)
            remaining_time_s -= remaining_time_m * 60
            LOGGER.info(
                f'remaining jobs to process for {target_stitch_raster_path}: '
                f'{n_expected-processed_so_far} - '
                f'processed so far {processed_so_far} - '
                f'process/sec: {jobs_per_sec:.1f}s - '
                f'time left: {remaining_time_h}:'
                f'{remaining_time_m:02d}:{remaining_time_s:04.1f}')
    except Exception:
        LOGGER.exception(
            f'error on stitch worker for {target_stitch_raster_path}')
        raise


def _run_ndr(
        task_graph,
        workspace_dir,
        watershed_path_list,
        dem_path,
        runoff_proxy_path,
        fertilizer_path,
        lulc_path,
        target_pixel_size,
        biophysical_table_path,
        biophysical_table_lucode_field,
        threshold_flow_accumulation,
        k_param,
        target_stitch_raster_map,
        global_pixel_size_deg,
        keep_intermediate_files=False,
        result_suffix=None,):

    # create intersecting bounding box of input data
    global_wgs84_bb = _calculate_intersecting_bounding_box(
        [dem_path, runoff_proxy_path, fertilizer_path, lulc_path])

    stitch_raster_queue_map = {}
    stitch_worker_list = []
    multiprocessing_manager = multiprocessing.Manager()
    signal_done_queue = multiprocessing_manager.Queue()
    for local_result_path, global_stitch_raster_path in \
            target_stitch_raster_map.items():
        if result_suffix is not None:
            global_stitch_raster_path = (
                f'%s_{result_suffix}%s' % os.path.splitext(
                    global_stitch_raster_path))
            local_result_path = (
                f'%s_{result_suffix}%s' % os.path.splitext(
                    local_result_path))
        if not os.path.exists(global_stitch_raster_path):
            LOGGER.info(f'creating {global_stitch_raster_path}')
            driver = gdal.GetDriverByName('GTiff')
            n_cols = int((global_wgs84_bb[2]-global_wgs84_bb[0])/global_pixel_size_deg)
            n_rows = int((global_wgs84_bb[3]-global_wgs84_bb[1])/global_pixel_size_deg)
            LOGGER.info(f'**** creating raster of size {n_cols} by {n_rows}')
            target_raster = driver.Create(
                global_stitch_raster_path,
                n_cols, n_rows, 1,
                gdal.GDT_Float32,
                options=(
                    'TILED=YES', 'BIGTIFF=YES', 'COMPRESS=LZW',
                    'SPARSE_OK=TRUE', 'BLOCKXSIZE=256', 'BLOCKYSIZE=256'))
            wgs84_srs = osr.SpatialReference()
            wgs84_srs.ImportFromEPSG(4326)
            target_raster.SetProjection(wgs84_srs.ExportToWkt())
            target_raster.SetGeoTransform(
                [global_wgs84_bb[0], global_pixel_size_deg, 0,
                 global_wgs84_bb[3], 0, -global_pixel_size_deg])
            target_band = target_raster.GetRasterBand(1)
            target_band.SetNoDataValue(-9999)
            target_raster = None
        stitch_queue = multiprocessing_manager.Queue(N_TO_BUFFER_STITCH*2)
        stitch_thread = threading.Thread(
            target=stitch_worker,
            args=(
                stitch_queue, global_stitch_raster_path,
                len(watershed_path_list),
                signal_done_queue))
        stitch_thread.start()
        stitch_raster_queue_map[local_result_path] = stitch_queue
        stitch_worker_list.append(stitch_thread)

    clean_workspace_worker = threading.Thread(
        target=_clean_workspace_worker,
        args=(
            len(target_stitch_raster_map), signal_done_queue,
            keep_intermediate_files))
    clean_workspace_worker.daemon = True
    clean_workspace_worker.start()

    # Iterate through each watershed subset and run ndr
    # stitch the results of whatever outputs to whatever global output raster.
    for index, watershed_path in enumerate(watershed_path_list):
        local_workspace_dir = os.path.join(
            workspace_dir, os.path.splitext(
                os.path.basename(watershed_path))[0])
        task_graph.add_task(
            func=_execute_ndr_job,
            args=(
                global_wgs84_bb, watershed_path, local_workspace_dir, dem_path,
                lulc_path, runoff_proxy_path, fertilizer_path,
                biophysical_table_path,
                threshold_flow_accumulation, k_param, target_pixel_size,
                biophysical_table_lucode_field, stitch_raster_queue_map,
                result_suffix),
            transient_run=False,
            priority=-index,  # priority in insert order
            task_name=f'ndr {os.path.basename(local_workspace_dir)}')

    LOGGER.info('wait for ndr jobs to complete')
    task_graph.join()
    for local_result_path, stitch_queue in stitch_raster_queue_map.items():
        stitch_queue.put(None)
    LOGGER.info('all done with ndr, waiting for stitcher to terminate')
    for stitch_thread in stitch_worker_list:
        stitch_thread.join()
    LOGGER.info(
        'all done with stitching, waiting for workspace worker to terminate')
    signal_done_queue.put(None)
    clean_workspace_worker.join()

    LOGGER.info('all done with ndr -- stitcher terminated')


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description='run NDR/SDR pipeline')
    parser.add_argument('config_file_path_pattern', nargs='+', help='Path to one or more .ini files or matching patterns')
    parser.add_argument('--min_watershed_area', default=0, type=float, help='Minimum watershed size to process in square degrees')
    args = parser.parse_args()

    default_config = configparser.ConfigParser(allow_no_value=True)
    default_config.read(
        os.path.join(os.path.dirname(__file__), 'global_config.ini'))
    expected_keys = _parse_non_default_options(default_config, 'expected_keys')

    config_file_list = [
        path for pattern in args.config_file_path_pattern
        for path in glob.glob(pattern)]
    if not config_file_list:
        raise ValueError(
            f'no config files were found from the input '
            f'{args.config_file_path_pattern}')
    scenario_list = []
    for config_path in config_file_list:
        scenario_id = os.path.basename(os.path.splitext(config_path)[0])
        scenario_config = configparser.ConfigParser(allow_no_value=True)
        scenario_config.read(config_path)
        if scenario_id not in scenario_config:
            raise ValueError(
                f'expected a section called "{scenario_id}" {config_path} but only found these section headers: {scenario_config.sections()}')
        missing_keys = []
        for expected_key in expected_keys:
            if expected_key not in scenario_config[scenario_id]:
                missing_keys.append(expected_key)
        if missing_keys:
            missing_key_str = ', '.join(missing_keys)
            raise ValueError(
                f'expected the following keys in {config_path} but were not found: {missing_key_str}')
        scenario_config.read('global_config.ini')
        scenario_list.append((scenario_id, scenario_config))

    LOGGER.debug(scenario_list)

    file_handler = None
    for scenario_id, scenario_config in scenario_list:
        if file_handler is not None:
            LOGGER.removeHandler(file_handler)
        workspace_dir = scenario_config.get(scenario_id, 'WORKSPACE_DIR')
        os.makedirs(workspace_dir, exist_ok=True)
        file_handler = logging.FileHandler(
            os.path.join(workspace_dir, f'{scenario_id}_log.txt'))
        LOGGER.addHandler(file_handler)
        run_scenario(workspace_dir, default_config, scenario_config, scenario_id, args.min_watershed_area)


def run_scenario(workspace_dir, default_config, scenario_config, scenario_id, min_watershed_area):
    """Run scenario `scenario_id` in config against taskgraph."""
    task_graph = taskgraph.TaskGraph(
        workspace_dir, multiprocessing.cpu_count(),
        15.0, parallel_mode='process', taskgraph_name=f'run pipeline {scenario_id}')
    expected_files = _parse_non_default_options(default_config, 'files')
    if not scenario_config.getboolean(scenario_id, 'run_ndr'):
        expected_files -= _parse_non_default_options(default_config, 'ndr_expected_keys')
    if not scenario_config.getboolean(scenario_id, 'run_sdr'):
        expected_files -= _parse_non_default_options(default_config, 'sdr_expected_keys')

    LOGGER.debug(expected_files)

    #data_map = fetch_and_unpack_data(task_graph, scenario_config, scenario_id)
    #LOGGER.debug(data_map)
    # make sure taskgraph doesn't re-run just because the file was opened
    watershed_subset_token_path = os.path.join(
        workspace_dir, f"{default_config['DEFAULT']['WATERSHED_SUBSET_TOKEN_PATH']}_{min_watershed_area}")
    exclusive_watershed_subset = scenario_config.get(
        scenario_id, 'watershed_subset', fallback=None)
    if exclusive_watershed_subset is not None:
        exclusive_watershed_subset = eval(exclusive_watershed_subset)
    watershed_subset_task = task_graph.add_task(
        func=_batch_into_watershed_subsets,
        args=(
            scenario_config[scenario_id]['WATERSHEDS'], 4,
            watershed_subset_token_path,
            eval(scenario_config.get('DEFAULT', 'GLOBAL_BB', fallback='None')),
            min_watershed_area,
            exclusive_watershed_subset),
        target_path_list=[watershed_subset_token_path],
        store_result=True,
        task_name='watershed subset batch')
    watershed_subset_list = watershed_subset_task.get()

    task_graph.join()

    sdr_target_stitch_raster_map = {
        'sed_export.tif': os.path.join(
            workspace_dir, 'stitched_sed_export.tif'),
        'sed_retention.tif': os.path.join(
            workspace_dir, 'stitched_sed_retention.tif'),
        'sed_deposition.tif': os.path.join(
            workspace_dir, 'stitched_sed_deposition.tif'),
        'usle.tif': os.path.join(
            workspace_dir, 'stitched_usle.tif'),
    }

    ndr_target_stitch_raster_map = {
        'n_export.tif': os.path.join(
            workspace_dir, 'stitched_n_export.tif'),
        'n_retention.tif': os.path.join(
            workspace_dir, 'stitched_n_retention.tif'),
        os.path.join('intermediate_outputs', 'modified_load_n.tif'): os.path.join(
            workspace_dir, 'stitched_modified_load_n.tif'),
    }

    config_section = scenario_config[scenario_id]
    run_sdr = scenario_config.getboolean(scenario_id, 'RUN_SDR')
    run_ndr = scenario_config.getboolean(scenario_id, 'RUN_NDR')
    keep_intermediate_files = scenario_config.getboolean(
        scenario_id, 'keep_intermediate_files')

    if run_sdr:
        sdr_workspace_dir = os.path.join(workspace_dir, 'sdr_workspace')
        os.makedirs(sdr_workspace_dir, exist_ok=True)
        # SDR doesn't have fert scenarios
        _run_sdr(
            task_graph=task_graph,
            workspace_dir=sdr_workspace_dir,
            watershed_path_list=watershed_subset_list,
            dem_path=config_section['DEM'],
            erosivity_path=config_section['EROSIVITY'],
            erodibility_path=config_section['ERODIBILITY'],
            lulc_path=config_section.get('LULC', None),
            usle_c_path=config_section.get('USLE_C', None),
            usle_p_path=config_section.get('USLE_P', None),
            target_pixel_size=float(config_section['TARGET_PIXEL_SIZE_M']),
            biophysical_table_path=config_section.get('BIOPHYSICAL_TABLE', None),
            biophysical_table_lucode_field=config_section.get(
                'BIOPHYSICAL_TABLE_LUCODE_COLUMN_ID', None),
            threshold_flow_accumulation=config_section.get(
                'THRESHOLD_FLOW_ACCUMULATION', None),
            l_cap=float(config_section['L_CAP']),
            k_param=float(config_section['K_PARAM']),
            sdr_max=float(config_section['SDR_MAX']),
            ic_0_param=float(config_section['IC_0_PARAM']),
            target_stitch_raster_map=sdr_target_stitch_raster_map,
            global_pixel_size_deg=float(
                config_section['GLOBAL_PIXEL_SIZE_DEG']),
            keep_intermediate_files=keep_intermediate_files,
            result_suffix=scenario_id)

    if run_ndr:
        ndr_workspace_dir = os.path.join(workspace_dir, 'ndr_workspace')
        os.makedirs(ndr_workspace_dir, exist_ok=True)
        _run_ndr(
            task_graph=task_graph,
            workspace_dir=ndr_workspace_dir,
            runoff_proxy_path=config_section['RUNOFF_PROXY'],
            fertilizer_path=config_section['FERTILIZER'],
            biophysical_table_path=config_section['BIOPHYSICAL_TABLE'],
            biophysical_table_lucode_field=config_section.get(scenario_id, 'BIOPHYSICAL_TABLE_LUCODE_COLUMN_ID'),
            watershed_path_list=watershed_subset_list,
            dem_path=config_section['DEM'],
            lulc_path=config_section['LULC'],
            target_pixel_size=config_section['TARGET_PIXEL_SIZE_M'],
            threshold_flow_accumulation=float(config_section['THRESHOLD_FLOW_ACCUMULATION']),
            k_param=config_section['K_PARAM'],
            target_stitch_raster_map=ndr_target_stitch_raster_map,
            global_pixel_size_deg=float(config_section['GLOBAL_PIXEL_SIZE_DEG']),
            keep_intermediate_files=keep_intermediate_files,
            result_suffix=scenario_id)
    task_graph.join()
    task_graph.close()


def _warp_raster_stack(
        task_graph, base_raster_path_list, warped_raster_path_list,
        resample_method_list, clip_pixel_size, target_pixel_size,
        clip_bounding_box, clip_projection_wkt, watershed_clip_vector_path):
    """Do an align of all the rasters but use a taskgraph to do it.

    Arguments are same as geoprocessing.align_and_resize_raster_stack.

    Allow for input rasters to be None.
    """
    for raster_path, warped_raster_path, resample_method in zip(
            base_raster_path_list, warped_raster_path_list,
            resample_method_list):
        if raster_path is None:
            continue
        working_dir = os.path.dirname(warped_raster_path)
        # first clip to clip projection
        clipped_raster_path = '%s_clipped%s' % os.path.splitext(
            warped_raster_path)
        task_graph.add_task(
            func=geoprocessing.warp_raster,
            args=(
                raster_path, clip_pixel_size, clipped_raster_path,
                resample_method),
            kwargs={
                'target_bb': clip_bounding_box,
                'target_projection_wkt': clip_projection_wkt,
                'working_dir': working_dir
            },
            target_path_list=[clipped_raster_path],
            task_name=f'clipping {clipped_raster_path}')

        # second, warp and mask to vector
        watershed_projection_wkt = geoprocessing.get_vector_info(
            watershed_clip_vector_path)['projection_wkt']

        vector_mask_options = {'mask_vector_path': watershed_clip_vector_path}
        task_graph.add_task(
            func=geoprocessing.warp_raster,
            args=(
                clipped_raster_path, (target_pixel_size, -target_pixel_size),
                warped_raster_path, resample_method,),
            kwargs={
                'target_projection_wkt': watershed_projection_wkt,
                'vector_mask_options': vector_mask_options,
                'working_dir': working_dir,
            },
            target_path_list=[warped_raster_path],
            task_name=f'warping {warped_raster_path}')


def _calculate_intersecting_bounding_box(raster_path_list):
    # create intersecting bounding box of input data
    raster_info_list = [
        geoprocessing.get_raster_info(raster_path)
        for raster_path in raster_path_list
        if raster_path is not None and
        geoprocessing.get_raster_info(raster_path)['projection_wkt']
        is not None]

    raster_bounding_box_list = [
        geoprocessing.transform_bounding_box(
            info['bounding_box'],
            info['projection_wkt'],
            osr.SRS_WKT_WGS84_LAT_LONG)
        for info in raster_info_list]

    target_bounding_box = geoprocessing.merge_bounding_box_list(
        raster_bounding_box_list, 'intersection')
    LOGGER.info(f'calculated target_bounding_box: {target_bounding_box}')
    return target_bounding_box


def _watersheds_intersect(wgs84_bb, watersheds_path):
    """True if watersheds intersect the wgs84 bounding box."""
    watershed_info = geoprocessing.get_vector_info(watersheds_path)
    watershed_wgs84_bb = geoprocessing.transform_bounding_box(
        watershed_info['bounding_box'],
        watershed_info['projection_wkt'],
        osr.SRS_WKT_WGS84_LAT_LONG)
    try:
        _ = geoprocessing.merge_bounding_box_list(
            [wgs84_bb, watershed_wgs84_bb], 'intersection')
        LOGGER.info(f'{watersheds_path} intersects {wgs84_bb} with {watershed_wgs84_bb}')
        return True
    except ValueError:
        LOGGER.warning(f'{watersheds_path} does not intersect {wgs84_bb}')
        return False


if __name__ == '__main__':
    main()
