# coding=utf-8
import gdal
import sys
import collections
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os
import zipfile
import shutil
import datetime
import scipy.sparse as sp
from datetime import date
import rasterio
import math
import copy
import seaborn as sns
from scipy.optimize import curve_fit
import time
from scipy import ndimage
from basic_function import Path
import basic_function as bf
from functools import wraps
import concurrent.futures
from itertools import repeat
from zipfile import ZipFile
import traceback
import osr
import shapely.geometry
import geog
import GEDI_process as gedi
from rasterstats import zonal_stats

# Input Snappy data style
np.seterr(divide='ignore', invalid='ignore')


def no_nan_mean(x):
    return np.nanmean(x)


def log_para(func):
    def wrapper(*args, **kwargs):
        pass

    return wrapper


def retrieve_srs(ds_temp):
    proj = osr.SpatialReference(wkt=ds_temp.GetProjection())
    srs_temp = proj.GetAttrValue('AUTHORITY', 1)
    srs_temp = 'EPSG:' + str(srs_temp)
    return srs_temp


def write_raster(ori_ds, new_array, file_path_f, file_name_f, raster_datatype=None, nodatavalue=None):
    if raster_datatype is None and nodatavalue is None:
        raster_datatype = gdal.GDT_Float32
        nodatavalue = np.nan
    elif raster_datatype is not None and nodatavalue is None:
        if raster_datatype is gdal.GDT_UInt16 or raster_datatype == 'UInt16':
            raster_datatype = gdal.GDT_UInt16
            nodatavalue = 65535
        elif raster_datatype is gdal.GDT_Int16 or raster_datatype == 'Int16':
            raster_datatype = gdal.GDT_Int16
            nodatavalue = -32768
        else:
            nodatavalue = 0
    elif raster_datatype is None and nodatavalue is not None:
        raster_datatype = gdal.GDT_Float32

    driver = gdal.GetDriverByName('GTiff')
    driver.Register()
    gt = ori_ds.GetGeoTransform()
    proj = ori_ds.GetProjection()
    if os.path.exists(file_path_f + file_name_f):
        os.remove(file_path_f + file_name_f)
    outds = driver.Create(file_path_f + file_name_f, xsize=new_array.shape[1], ysize=new_array.shape[0], bands=1,
                          eType=raster_datatype, options=['COMPRESS=LZW', 'PREDICTOR=2'])
    outds.SetGeoTransform(gt)
    outds.SetProjection(proj)
    outband = outds.GetRasterBand(1)
    outband.WriteArray(new_array)
    outband.SetNoDataValue(nodatavalue)
    outband.FlushCache()
    outband = None
    outds = None


def eliminating_all_not_required_file(file_path_f, filename_extension=None):
    if filename_extension is None:
        filename_extension = ['txt', 'tif', 'TIF', 'json', 'jpeg', 'xml']
    filter_name = ['.']
    tif_file_list = bf.file_filter(file_path_f, filter_name)
    for file in tif_file_list:
        if file.split('.')[-1] not in filename_extension:
            try:
                os.remove(file)
            except:
                raise Exception(f'file {file} cannot be removed')

        if str(file[-8:]) == '.aux.xml':
            try:
                os.remove(file)
            except:
                raise Exception(f'file {file} cannot be removed')


def union_list(small_list, big_list):
    union_list_temp = []
    if type(small_list) != list or type(big_list) != list:
        print('Please input valid lists')
        sys.exit(-1)

    for i in small_list:
        if i not in big_list:
            print(f'{i} is not supported!')
        else:
            union_list_temp.append(i)
    return union_list_temp


class Sentinel2_ds(object):

    def __init__(self, ori_zipfile_folder, work_env=None):
        # Define var
        self.S2_metadata = None
        self._subset_failure_file = []
        self._index_construction_failure_file = []
        self.output_bounds = np.array([])
        self.raw_10m_bounds = np.array([])
        self.ROI = None
        self.ROI_name = None
        self.ori_folder = Path(ori_zipfile_folder).path_name
        self.S2_metadata_size = np.nan
        self.date_list = []
        self.main_coordinate_system = None

        # Define key variables (kwargs)
        self._size_control_factor = False
        self._cloud_removal_para = False
        self._vi_clip_factor = False
        self._sparsify_matrix_factor = False
        self._cloud_clip_seq = None

        # Remove all the duplicated data
        dup_data = bf.file_filter(self.ori_folder, ['.1.zip'])
        for dup in dup_data:
            os.remove(dup)

        # Generate the original zip file list
        self.orifile_list = bf.file_filter(self.ori_folder, ['.zip', 'S2'], and_or_factor='and', subfolder_detection=True)
        self.orifile_list = [i for i in self.orifile_list if 'S2' in i.split('\\')[-1] and '.zip' in i.split('\\')[-1]]
        if not self.orifile_list:
            print('There has no Sentinel zipfiles in the input dir')
            sys.exit(-1)

        # Initialise the work environment
        if work_env is None:
            try:
                self.work_env = Path(os.path.dirname(os.path.dirname(self.ori_folder)) + '\\').path_name
            except:
                print('There has no base dir for the ori_folder and the ori_folder will be treated as the work env')
                self.work_env = self.ori_folder
        else:
            self.work_env = Path(work_env).path_name

        # Create cache path
        self.cache_folder = self.work_env + 'cache\\'
        bf.create_folder(self.cache_folder)
        self.trash_folder = self.work_env + 'trash\\'
        bf.create_folder(self.trash_folder)
        bf.create_folder(self.work_env + 'Corrupted_S2_file\\')

        # Create output path
        self.output_path = f'{self.work_env}Sentinel2_L2A_Output\\'
        self.shpfile_path = f'{self.work_env}shpfile\\'
        self.log_filepath = f'{self.work_env}logfile\\'
        bf.create_folder(self.output_path)
        bf.create_folder(self.log_filepath)
        bf.create_folder(self.shpfile_path)

        # define 2dc para
        self.dc_vi = {}
        self._dc_overwritten_para = False
        self._inherit_from_logfile = None
        self._remove_nan_layer = False
        self._manually_remove_para = False
        self._manually_remove_datelist = None

        # Constant
        self.band_name_list = ['B01_60m.jp2', 'B02_10m.jp2', 'B03_10m.jp2', 'B04_10m.jp2', 'B05_20m.jp2', 'B06_20m.jp2',
                               'B07_20m.jp2', 'B8A_20m.jp2', 'B09_60m.jp2', 'B11_20m.jp2', 'B12_20m.jp2']
        self.band_output_list = ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B9', 'B11', 'B12']
        self.all_supported_index_list = ['RGB', 'QI', 'all_band', '4visual', 'NDVI', 'MNDWI', 'EVI', 'EVI2', 'OSAVI', 'GNDVI',
                                         'NDVI_RE', 'NDVI_RE2', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B9',
                                         'B11', 'B12']

    def save_log_file(func):
        def wrapper(self, *args, **kwargs):

            #########################################################################
            # Document the log file and para file
            # The difference between log file and para file is that the log file contains the information for each run/debug
            # While the para file only comprises of the parameter for the latest run/debug
            #########################################################################

            time_start = time.time()
            c_time = time.ctime()
            log_file = open(f"{self.log_filepath}log.txt", "a+")
            if os.path.exists(f"{self.log_filepath}para_file.txt"):
                para_file = open(f"{self.log_filepath}para_file.txt", "r+")
            else:
                para_file = open(f"{self.log_filepath}para_file.txt", "w+")
            error_inf = None

            para_txt_all = para_file.read()
            para_ori_txt = para_txt_all.split('#' * 70 + '\n')
            para_txt = para_txt_all.split('\n')
            contain_func = [txt for txt in para_txt if txt.startswith('Process Func:')]

            try:
                func(self, *args, **kwargs)
            except:
                error_inf = traceback.format_exc()
                print(error_inf)

            # Header for the log file
            log_temp = ['#' * 70 + '\n', f'Process Func: {func.__name__}\n', f'Start time: {c_time}\n',
                    f'End time: {time.ctime()}\n', f'Total processing time: {str(time.time() - time_start)}\n']

            # Create args and kwargs list
            args_f = 0
            args_list = ['*' * 25 + 'Arguments' + '*' * 25 + '\n']
            kwargs_list = []
            for i in args:
                args_list.extend([f"args{str(args_f)}:{str(i)}\n"])
            for k_key in kwargs.keys():
                kwargs_list.extend([f"{str(k_key)}:{str(kwargs[k_key])}\n"])
            para_temp = ['#' * 70 + '\n', f'Process Func: {func.__name__}\n', f'Start time: {c_time}\n',
                    f'End time: {time.ctime()}\n', f'Total processing time: {str(time.time() - time_start)}\n']
            para_temp.extend(args_list)
            para_temp.extend(kwargs_list)
            para_temp.append('#' * 70 + '\n')

            log_temp.extend(args_list)
            log_temp.extend(kwargs_list)
            log_file.writelines(log_temp)
            for func_key, func_processing_name in zip(['metadata', 'subset', 'datacube'], ['constructing metadata', 'executing subset and clip', '2dc']):
                if func_key in func.__name__:
                    if error_inf is None:
                        log_file.writelines([f'Status: Finished {func_processing_name}!\n', '#' * 70 + '\n'])
                        metadata_line = [q for q in contain_func if func_key in q]
                        if len(metadata_line) == 0:
                            para_file.writelines(para_temp)
                            para_file.close()
                        elif len(metadata_line) == 1:
                            for para_ori_temp in para_ori_txt:
                                if para_ori_temp != '' and metadata_line[0] not in para_ori_temp:
                                    para_temp.extend(['#' * 70 + '\n', para_ori_temp, '#' * 70 + '\n'])
                                    para_file.close()
                                    para_file = open(f"{self.log_filepath}para_file.txt", "w+")
                                    para_file.writelines(para_temp)
                                    para_file.close()
                        elif len(metadata_line) > 1:
                            print('Code error! ')
                            sys.exit(-1)
                    else:
                        log_file.writelines([f'Status: Error in {func_processing_name}!\n', 'Error information:\n', error_inf + '\n', '#' * 70 + '\n'])
        return wrapper

    def _retrieve_para(self, required_para_name_list, **kwargs):

        if not os.path.exists(f'{self.log_filepath}para_file.txt'):
            print('The para file is not established yet')
            sys.exit(-1)
        else:
            para_file = open(f"{self.log_filepath}para_file.txt", "r+")
            para_raw_txt = para_file.read().split('\n')

        for para in required_para_name_list:
            if para in self.__dir__():
                for q in para_raw_txt:
                    para = str(para)
                    if q.startswith(para + ':'):
                        if q.split(para + ':')[-1] == 'None':
                            self.__dict__[para] = None
                        elif q.split(para + ':')[-1] == 'True':
                            self.__dict__[para] = True
                        elif q.split(para + ':')[-1] == 'False':
                            self.__dict__[para] = False
                        elif q.split(para + ':')[-1].startswith('['):
                            self.__dict__[para] = list(q.split(para + ':')[-1][1: -1])
                        elif q.split(para + ':')[-1].startswith('('):
                            self.__dict__[para] = tuple(q.split(para + ':')[-1][1: -1])
                        else:
                            try:
                                t = float(q.split(para + ':')[-1])
                                self.__dict__[para] = float(q.split(para + ':')[-1])
                            except:
                                self.__dict__[para] = q.split(para + ':')[-1]

    @save_log_file
    def construct_metadata(self):

        # Start constructing metadata
        print('---------------------------- Start the construction of Metadata ----------------------------')
        start_temp = time.time()

        # process input files
        if os.path.exists(self.work_env + 'Metadata.xlsx'):
            metadata_num = pd.read_excel(self.work_env + 'Metadata.xlsx').shape[0]
        else:
            metadata_num = 0

        if not os.path.exists(self.work_env + 'Metadata.xlsx') or metadata_num != len(self.orifile_list):
            corrupted_ori_file, corrupted_file_date, product_path, product_name, sensor_type, sensing_date, orbit_num, tile_num, width, height = (
                [] for i in range(10))
            corrupted_factor = 0
            for ori_file in self.orifile_list:
                try:
                    unzip_file = zipfile.ZipFile(ori_file)
                    unzip_file.close()
                    file_name = ori_file.split('\\')[-1]
                    product_path.append(ori_file)
                    sensing_date.append(file_name[file_name.find('_20') + 1: file_name.find('_20') + 9])
                    orbit_num.append(file_name[file_name.find('_R') + 2: file_name.find('_R') + 5])
                    tile_num.append(file_name[file_name.find('_T') + 2: file_name.find('_T') + 7])
                    sensor_type.append(file_name[file_name.find('S2'): file_name.find('S2') + 10])
                    # print(file_information)
                except:
                    if (not os.path.exists(self.work_env + 'Corrupted_S2_file')) and corrupted_factor == 0:
                        os.makedirs(self.work_env + 'Corrupted_S2_file')
                        corrupted_factor = 1
                    print(f'This file is corrupted {ori_file}!')
                    file_name = ori_file.split('\\')[-1]
                    corrupted_ori_file.append(file_name)

                    corrupted_file_date.append(file_name[file_name.find('_20') + 1: file_name.find('_20') + 9])
                    shutil.move(ori_file, self.work_env + 'Corrupted_S2_file\\' + file_name)

            # Construct corrupted metadata
            Corrupted_metadata = pd.DataFrame({'Corrupted_file_name': corrupted_ori_file, 'File_Date': corrupted_file_date})
            if not os.path.exists(self.work_env + 'Corrupted_metadata.xlsx'):
                Corrupted_metadata.to_excel(self.work_env + 'Corrupted_metadata.xlsx')
            else:
                Corrupted_metadata_old_version = pd.read_excel(self.work_env + 'Corrupted_metadata.xlsx')
                Corrupted_metadata_old_version.append(Corrupted_metadata, ignore_index=True)
                Corrupted_metadata_old_version.drop_duplicates()
                Corrupted_metadata_old_version.to_excel(self.work_env + 'Corrupted_metadata.xlsx')

            self.S2_metadata = pd.DataFrame({'Product_Path': product_path, 'Sensing_Date': sensing_date,
                                             'Orbit_Num': orbit_num, 'Tile_Num': tile_num, 'Sensor_Type': sensor_type})

            # Process duplicate file
            duplicate_file_list = []
            i = 0
            while i <= self.S2_metadata.shape[0] - 1:
                file_inform = str(self.S2_metadata['Sensing_Date'][i]) + '_' + self.S2_metadata['Tile_Num'][i]
                q = i + 1
                while q <= self.S2_metadata.shape[0] - 1:
                    file_inform2 = str(self.S2_metadata['Sensing_Date'][q]) + '_' + self.S2_metadata['Tile_Num'][q]
                    if file_inform2 == file_inform:
                        if len(self.S2_metadata['Product_Path'][i]) > len(self.S2_metadata['Product_Path'][q]):
                            duplicate_file_list.append(self.S2_metadata['Product_Path'][i])
                        elif len(self.S2_metadata['Product_Path'][i]) < len(self.S2_metadata['Product_Path'][q]):
                            duplicate_file_list.append(self.S2_metadata['Product_Path'][q])
                        else:
                            if int(os.path.getsize(self.S2_metadata['Product_Path'][i])) > int(os.path.getsize(self.S2_metadata['Product_Path'][q])):
                                duplicate_file_list.append(self.S2_metadata['Product_Path'][q])
                            else:
                                duplicate_file_list.append(self.S2_metadata['Product_Path'][i])
                        break
                    q += 1
                i += 1

            duplicate_file_list = list(dict.fromkeys(duplicate_file_list))
            if duplicate_file_list != []:
                for file in duplicate_file_list:
                    shutil.move(file, self.work_env + 'Corrupted_S2_file\\' + file.split('\\')[-1])
                self.construct_metadata()
            else:
                self.S2_metadata.to_excel(self.work_env + 'Metadata.xlsx')
                self.S2_metadata = pd.read_excel(self.work_env + 'Metadata.xlsx')
        else:
            self.S2_metadata = pd.read_excel(self.work_env + 'Metadata.xlsx')
        self.S2_metadata.sort_values(by=['Sensing_Date'], ascending=True)
        self.S2_metadata_size = self.S2_metadata.shape[0]
        self.output_bounds = np.zeros([self.S2_metadata_size, 4]) * np.nan
        self.raw_10m_bounds = np.zeros([self.S2_metadata_size, 4]) * np.nan
        self.date_list = self.S2_metadata['Sensing_Date'].drop_duplicates().to_list()
        print(f'Finish in {str(time.time() - start_temp)} sec!')
        print('----------------------------  End the construction of Metadata  ----------------------------')

    def _qi_remove_cloud(self, processed_filepath, qi_filepath, serial_num, dst_nodata=0, **kwargs):
        # Determine the process parameter
        sensing_date = self.S2_metadata['Sensing_Date'][serial_num]
        tile_num = self.S2_metadata['Tile_Num'][serial_num]
        if kwargs['cloud_removal_strategy'] == 'QI_all_cloud':
            cloud_indicator = [0, 1, 2, 3, 8, 9, 10, 11]
        else:
            print('Cloud removal strategy is not supported!')
            sys.exit(-1)

        # Input ds
        try:
            qi_ds = gdal.Open(qi_filepath)
            processed_ds = gdal.Open(processed_filepath, gdal.GA_Update)
            qi_array = qi_ds.GetRasterBand(1).ReadAsArray()
            processed_array = processed_ds.GetRasterBand(1).ReadAsArray()
        except:
            self._subset_failure_file.append(['QI', serial_num, sensing_date, tile_num])
            print('QI')
            return

        if qi_array is None or processed_array is None:
            self._subset_failure_file.append(['QI', serial_num, sensing_date, tile_num])
            print('QI')
            return
        elif qi_array.shape[0] != processed_array.shape[0] or qi_array.shape[1] != processed_array.shape[1]:
            print('Consistency error')
            sys.exit(-1)

        for indicator in cloud_indicator:
            qi_array[qi_array == indicator] = 64
        qi_array[qi_array == 255] = 64
        processed_array[qi_array == 64] = dst_nodata

        processed_ds.GetRasterBand(1).WriteArray(processed_array)
        processed_ds.FlushCache()
        processed_ds = None
        qi_ds = None

    def _check_metadata_availability(self):
        if self.S2_metadata is None:
            try:
                self.construct_metadata()
            except:
                print('Please manually construct the S2_metadata before further processing!')
                sys.exit(-1)

    def _check_output_band_statue(self, band_name, tiffile_serial_num, *args, **kwargs):

        # Define local var
        sensing_date = self.S2_metadata['Sensing_Date'][tiffile_serial_num]
        tile_num = self.S2_metadata['Tile_Num'][tiffile_serial_num]

        # Factor configuration
        if True in [band_temp not in self.band_output_list for band_temp in band_name]:
            print(f'Band {band_name} is not valid!')
            sys.exit(-1)

        if self._vi_clip_factor:
            output_path = f'{self.output_path}Sentinel2_{self.ROI_name}_index\\all_band\\'
        else:
            output_path = f'{self.output_path}Sentinel2_constructed_index\\all_band\\'
        bf.create_folder(output_path)

        # Detect whether the required band was generated before
        try:
            if False in [os.path.exists(f'{output_path}{str(sensing_date)}_{str(tile_num)}_{band_temp}.TIF') for band_temp in band_name]:
                self.subset_tiffiles(band_name, tiffile_serial_num, **kwargs)

            # Return output
            if False in [os.path.exists(f'{output_path}{str(sensing_date)}_{str(tile_num)}_{band_temp}.TIF') for band_temp in band_name]:
                print(f'Something error processing {band_name}!')
                return None
            else:
                return [gdal.Open(f'{output_path}{str(sensing_date)}_{str(tile_num)}_{band_temp}.TIF') for band_temp in band_name]

        except:
            return None

    @save_log_file
    def mp_subset(self, *args, **kwargs):
        if self.S2_metadata is None:
            print('Please construct the S2_metadata before the subset!')
            sys.exit(-1)
        i = range(self.S2_metadata.shape[0])
        # mp process
        with concurrent.futures.ProcessPoolExecutor() as executor:
            executor.map(self.subset_tiffiles, repeat(args[0]), i, repeat(False), repeat(kwargs))
        self._process_subset_failure_file(args[0])

    @save_log_file
    def sequenced_subset(self, *args, **kwargs):
        if self.S2_metadata is None:
            print('Please construct the S2_metadata before the subset!')
            sys.exit(-1)
        # sequenced process
        for i in range(self.S2_metadata.shape[0]):
            self.subset_tiffiles(args[0], i, **kwargs)
        self._process_subset_failure_file(args[0])

    def _subset_indicator_process(self, **kwargs):
        # Detect whether all the indicator are valid
        for kwarg_indicator in kwargs.keys():
            if kwarg_indicator not in ['ROI', 'ROI_name', 'size_control_factor', 'cloud_removal_strategy', 'combine_band_factor']:
                print(f'{kwarg_indicator} is not supported kwargs! Please double check!')

        # process clip parameter
        if self.ROI is None:
            if 'ROI' in kwargs.keys():
                self._vi_clip_factor = True
                if '.shp' in kwargs['ROI'] and os.path.exists(kwargs['ROI']):
                    self.ROI = kwargs['ROI']
                else:
                    print('Please input valid shp file for clip!')
                    sys.exit(-1)

                if 'ROI_name' in kwargs.keys():
                    self.ROI_name = kwargs['ROI_name']
                else:
                    self.ROI_name = self.ROI.split('\\')[-1].split('.')[0]
            else:
                self._vi_clip_factor = False

        # process size control parameter
        if 'size_control_factor' in kwargs.keys():
            if type(kwargs['size_control_factor']) is bool:
                self._size_control_factor = kwargs['size_control_factor']
            else:
                print('Please mention the size_control_factor should be bool type!')
                self._size_control_factor = False
        else:
            self._size_control_factor = False

        # process cloud removal parameter
        if 'cloud_removal_strategy' in kwargs.keys():
            self._cloud_removal_para = True
        else:
            self._cloud_removal_para = False

        # process main_coordinate_system
        if 'main_coordinate_system' in kwargs.keys():
            self.main_coordinate_system = kwargs['main_coordinate_system']
        else:
            self.main_coordinate_system = 'EPSG:32649'

        # process combine band factor
        if 'combine_band_factor' in kwargs.keys():
            if type(kwargs['combine_band_factor']) is bool:
                self._combine_band_factor = kwargs['combine_band_factor']
            else:
                raise Exception(f'combine band factor is not under the bool type!')
        else:
            self._combine_band_factor = False


    def generate_10m_output_bounds(self, tiffile_serial_num, **kwargs):

        # Define local var
        topts = gdal.TranslateOptions(creationOptions=['COMPRESS=LZW', 'PREDICTOR=2'])
        sensing_date = self.S2_metadata['Sensing_Date'][tiffile_serial_num]
        tile_num = self.S2_metadata['Tile_Num'][tiffile_serial_num]
        VI = 'all_band'

        # Define the output path
        if self._vi_clip_factor:
            output_path = f'{self.output_path}Sentinel2_{self.ROI_name}_index\\{VI}\\'
        else:
            output_path = f'{self.output_path}Sentinel2_constructed_index\\{VI}\\'
        bf.create_folder(output_path)

        # Create the output bounds based on the 10-m Band2 images
        if self.output_bounds.shape[0] > tiffile_serial_num:
            if True in np.isnan(self.output_bounds[tiffile_serial_num, :]):
                temp_S2file_path = self.S2_metadata.iat[tiffile_serial_num, 1]
                zfile = ZipFile(temp_S2file_path, 'r')
                b2_band_file_name = f'{str(sensing_date)}_{str(tile_num)}_B2'
                if not os.path.exists(output_path + b2_band_file_name + '.TIF'):
                    b2_file = [zfile_temp for zfile_temp in zfile.namelist() if 'B02_10m.jp2' in zfile_temp]
                    if len(b2_file) != 1:
                        print(f'Data issue for the B2 file of all_cloud data ({str(tiffile_serial_num + 1)} of {str(self.S2_metadata.shape[0])})')
                        self._subset_failure_file.append([VI, tiffile_serial_num, sensing_date, tile_num])
                        return
                    else:
                        try:
                            ds_temp = gdal.Open('/vsizip/%s/%s' % (temp_S2file_path, b2_file[0]))
                            ulx_temp, xres_temp, xskew_temp, uly_temp, yskew_temp, yres_temp = ds_temp.GetGeoTransform()
                            self.output_bounds[tiffile_serial_num, :] = np.array([ulx_temp, uly_temp + yres_temp * ds_temp.RasterYSize, ulx_temp + xres_temp * ds_temp.RasterXSize, uly_temp])
                            band_output_limit = (int(self.output_bounds[tiffile_serial_num, 0]), int(self.output_bounds[tiffile_serial_num, 1]),
                                                 int(self.output_bounds[tiffile_serial_num, 2]), int(self.output_bounds[tiffile_serial_num, 3]))
                            if self._vi_clip_factor:
                                gdal.Warp('/vsimem/' + b2_band_file_name + '.TIF', ds_temp,
                                          dstSRS=self.main_coordinate_system, xRes=10, yRes=10, cutlineDSName=self.ROI,
                                          outputType=gdal.GDT_UInt16, dstNodata=65535, outputBounds=band_output_limit)
                            else:
                                gdal.Warp('/vsimem/' + b2_band_file_name + '.TIF', ds_temp,
                                          dstSRS=self.main_coordinate_system, xRes=10, yRes=10,
                                          outputType=gdal.GDT_UInt16, dstNodata=65535, outputBounds=band_output_limit)
                            gdal.Translate(output_path + b2_band_file_name + '.TIF', '/vsimem/' + b2_band_file_name + '.TIF', options=topts, noData=65535)
                            gdal.Unlink('/vsimem/' + b2_band_file_name + '.TIF')
                        except:
                            self._subset_failure_file.append([VI, tiffile_serial_num, sensing_date, tile_num])
                            print(f'The B2 of {str(sensing_date)}_{str(tile_num)} is not valid')
                            return
                else:
                    ds4bounds = gdal.Open(output_path + b2_band_file_name + '.TIF')
                    ulx, xres, xskew, uly, yskew, yres = ds4bounds.GetGeoTransform()
                    self.output_bounds[tiffile_serial_num, :] = np.array(
                        [ulx, uly + yres * ds4bounds.RasterYSize, ulx + xres * ds4bounds.RasterXSize, uly])
                    ds4bounds = None
        else:
            print('The output bounds has some logical issue!')
            sys.exit(-1)

    def _process_subset_failure_file(self, index_list, **kwargs):
        if self._subset_failure_file != []:
            subset_failure_file_folder = self.work_env + 'Corrupted_S2_file\\subset_failure_file\\'
            bf.create_folder(subset_failure_file_folder)
            for subset_failure_file in self._subset_failure_file:
                # remove all the related file
                related_output_file = bf.file_filter(self.output_path, [subset_failure_file[2], subset_failure_file[3]], and_or_factor='and', subfolder_detection=True)
                for file in related_output_file:
                    file_name = file.split('\\')[-1]
                    shutil.move(file, f'{self.trash_folder}{file_name}')

            shutil.rmtree(self.trash_folder)
            bf.create_folder(self.trash_folder)
            try:
                for subset_failure_file in self._subset_failure_file:
                    self.subset_tiffiles(index_list, subset_failure_file[1], **kwargs)
            except:
                print(f'The {str(subset_failure_file[1])} is not processed due to code issue!')

            for subset_failure_file in self._subset_failure_file:
                # remove all the related file
                related_output_file = bf.file_filter(self.output_path, [subset_failure_file[2], subset_failure_file[3]], and_or_factor='and', subfolder_detection=True)
                for file in related_output_file:
                    file_name = file.split('\\')[-1]
                    shutil.move(file, f'{self.trash_folder}{file_name}')

                # remove all the zip file
                zipfile = bf.file_filter(self.ori_folder, [subset_failure_file[2], subset_failure_file[3]], and_or_factor='and')
                for file in zipfile:
                    file_name = file.split('\\')[-1]
                    shutil.move(file, f'{subset_failure_file_folder}{file_name}')
            self.construct_metadata()

    def subset_tiffiles(self, processed_index_list, tiffile_serial_num, overwritten_para=False, *args, **kwargs):
        """
        :type processed_index_list: list
        :type tiffile_serial_num: int
        :type overwritten_para: bool

        """
        # subset_tiffiles is the core function in subsetting, resampling, clipping images as well as extracting VI and removing clouds.
        # The argument includes
        # ROI = define the path of a .shp file using for clipping all the sentinel-2 images
        # ROI_name = using to generate the roi-specified output folder, the default value is setting as the name of the ROI shp file
        # cloud_remove_strategy = method using to remove clouds, supported methods include QI_all_cloud

        # Define local args
        topts = gdal.TranslateOptions(creationOptions=['COMPRESS=LZW', 'PREDICTOR=2'])
        time1, time2, time3 = 0, 0, 0

        # Retrieve kwargs from args using the mp
        if args != () and type(args[0]) == dict:
            kwargs = copy.copy(args[0])

        # determine the subset indicator
        self._check_metadata_availability()
        self._subset_indicator_process(**kwargs)

        # Process subset index list
        processed_index_list = union_list(processed_index_list, self.all_supported_index_list)
        combine_index_list = []
        if self._combine_band_factor:
            for q in processed_index_list:
                if q in ['NDVI', 'MNDWI', 'EVI', 'EVI2', 'OSAVI', 'GNDVI', 'NDVI_RE', 'NDVI_RE2', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B9', 'B11', 'B12', 'QI']:
                    combine_index_list.append(q)
                elif q in ['RGB', 'all_band', '4visual']:
                    combine_index_list.extend([['B2', 'B3', 'B4'], ['B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B9', 'B11', 'B12'], ['B2', 'B3', 'B4', 'B5', 'B8', 'B11']][['RGB', 'all_band', '4visual'].index(q)])
            combine_index_array_list = copy.copy(combine_index_list)

        if processed_index_list != []:
            # Generate the output boundary
            self.generate_10m_output_bounds(tiffile_serial_num, **kwargs)

            temp_S2file_path = self.S2_metadata.iat[tiffile_serial_num, 1]
            zfile = ZipFile(temp_S2file_path, 'r')
            band_output_limit = (int(self.output_bounds[tiffile_serial_num, 0]), int(self.output_bounds[tiffile_serial_num, 1]),
                                 int(self.output_bounds[tiffile_serial_num, 2]), int(self.output_bounds[tiffile_serial_num, 3]))

            for index in processed_index_list:
                start_temp = time.time()
                print(f'Start processing {index} data ({str(tiffile_serial_num + 1)} of {str(self.S2_metadata_size)})')
                sensing_date = self.S2_metadata['Sensing_Date'][tiffile_serial_num]
                tile_num = self.S2_metadata['Tile_Num'][tiffile_serial_num]

                # Generate output folder
                if self._vi_clip_factor:
                    subset_output_path = f'{self.output_path}Sentinel2_{self.ROI_name}_index\\{index}\\'
                    if index in self.band_output_list or index in ['4visual', 'RGB']:
                        subset_output_path = f'{self.output_path}Sentinel2_{self.ROI_name}_index\\all_band\\'
                else:
                    subset_output_path = f'{self.output_path}Sentinel2_constructed_index\\{index}\\'
                    if index in self.band_output_list or index in ['4visual', 'RGB']:
                        subset_output_path = f'{self.output_path}Sentinel2_constructed_index\\all_band\\'

                # Generate qi output folder
                if self._cloud_clip_seq or not self._vi_clip_factor:
                    qi_path = f'{self.output_path}Sentinel2_constructed_index\\QI\\'
                else:
                    qi_path = f'{self.output_path}Sentinel2_{self.ROI_name}_index\\QI\\'

                if self._combine_band_factor:
                    folder_name = ''
                    for combine_index_temp in combine_index_list:
                        folder_name = folder_name + str(combine_index_temp) + '_'
                    if self._cloud_clip_seq or not self._vi_clip_factor:
                        combine_band_folder = f'{self.output_path}Sentinel2_constructed_index\\' + folder_name[0:-1] + '\\'
                    else:
                        combine_band_folder = f'{self.output_path}Sentinel2_{self.ROI_name}_index\\' + folder_name[0:-1] + '\\'
                    bf.create_folder(combine_band_folder)
                    if os.path.exists(f'{combine_band_folder}{str(sensing_date)}_{str(tile_num)}.TIF'):
                        self._combine_band_factor, combine_index_list = False, []

                bf.create_folder(subset_output_path)
                bf.create_folder(qi_path)

                # Define the file name for VI
                file_name = f'{str(sensing_date)}_{str(tile_num)}_{index}'

                # Generate QI layer
                if (index == 'QI' and overwritten_para) or (index == 'QI' and not overwritten_para and not os.path.exists(qi_path + file_name + '.TIF')):
                    band_all = [zfile_temp for zfile_temp in zfile.namelist() if 'SCL_20m.jp2' in zfile_temp]
                    if len(band_all) != 1:
                        print(f'Something error during processing {index} data ({str(tiffile_serial_num + 1)} of {str(self.S2_metadata_size)})')
                        self._subset_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                    else:
                        for band_temp in band_all:
                            try:
                                ds_temp = gdal.Open('/vsizip/%s/%s' % (temp_S2file_path, band_temp))
                                if self._vi_clip_factor:
                                    gdal.Warp('/vsimem/' + file_name + 'temp.TIF', ds_temp, xRes=10, yRes=10, dstSRS=self.main_coordinate_system, outputBounds=band_output_limit, outputType=gdal.GDT_Byte, dstNodata=255)
                                    gdal.Warp('/vsimem/' + file_name + '.TIF', '/vsimem/' + file_name + 'temp.TIF', xRes=10, yRes=10, outputBounds=band_output_limit, cutlineDSName=self.ROI)
                                else:
                                    gdal.Warp('/vsimem/' + file_name + '.TIF', ds_temp, xRes=10, yRes=10, dstSRS=self.main_coordinate_system, outputBounds=band_output_limit, outputType=gdal.GDT_Byte, dstNodata=255)
                                gdal.Translate(qi_path + file_name + '.TIF', '/vsimem/' + file_name + '.TIF', options=topts, noData=255, outputType=gdal.GDT_Byte)

                                if self._combine_band_factor and 'QI' in combine_index_list:
                                    temp_ds = gdal.Open(qi_path + file_name + '.TIF')
                                    temp_array = temp_ds.GetRasterBand(1).ReadAsArray()
                                    temp_array = temp_array.astype(np.float)
                                    temp_array[temp_array == 255] = np.nan
                                    combine_index_array_list[combine_index_list.index('QI')] = temp_array
                                gdal.Unlink('/vsimem/' + file_name + '.TIF')

                            except:
                                self._subset_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                                print(f'The {index} of {str(sensing_date)}_{str(tile_num)} is not valid')
                                return

                elif index == 'QI' and os.path.exists(qi_path + file_name + '.TIF') and 'QI' in combine_index_list:
                    temp_ds = gdal.Open(qi_path + file_name + '.TIF')
                    temp_array = temp_ds.GetRasterBand(1).ReadAsArray()
                    temp_array = temp_array.astype(np.float)
                    temp_array[temp_array == 255] = np.nan
                    combine_index_array_list[combine_index_list.index('QI')] = temp_array

                # Subset band images
                elif index == 'all_band' or index == '4visual' or index == 'RGB' or index in self.band_output_list:
                    # Check the output band
                    if index == 'all_band':
                        band_name_list, band_output_list = self.band_name_list, self.band_output_list
                    elif index == '4visual':
                        band_name_list, band_output_list = ['B02_10m.jp2', 'B03_10m.jp2', 'B04_10m.jp2', 'B05_20m.jp2','B8A_20m.jp2', 'B11_20m.jp2'], ['B2', 'B3', 'B4', 'B5','B8', 'B11']
                    elif index == 'RGB':
                        band_name_list, band_output_list = ['B02_10m.jp2', 'B03_10m.jp2', 'B04_10m.jp2'], ['B2', 'B3', 'B4']
                    elif index in self.band_output_list:
                        band_name_list, band_output_list = [self.band_name_list[self.band_output_list.index(index)]], [index]
                    else:
                        print('Code error!')
                        sys.exit(-1)

                    if overwritten_para or False in [os.path.exists(subset_output_path + str(sensing_date) + '_' + str(tile_num) + '_' + str(band_temp) + '.TIF') for band_temp in band_output_list] or (self._combine_band_factor and True in [band_index_temp in band_output_list for band_index_temp in combine_index_list]):
                        for band_name, band_output in zip(band_name_list, band_output_list):
                            if band_output != 'B2':
                                all_band_file_name = f'{str(sensing_date)}_{str(tile_num)}_{str(band_output)}'
                                if not os.path.exists(subset_output_path + all_band_file_name + '.TIF') or overwritten_para:
                                    band_all = [zfile_temp for zfile_temp in zfile.namelist() if band_name in zfile_temp]
                                    if len(band_all) != 1:
                                        print(f'Something error during processing {band_output} of {index} data ({str(tiffile_serial_num + 1)} of {str(self.S2_metadata_size)})')
                                        self._subset_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                                    else:
                                        for band_temp in band_all:
                                            try:
                                                ds_temp = gdal.Open('/vsizip/%s/%s' % (temp_S2file_path, band_temp))
                                                t1 = time.time()
                                                if self._vi_clip_factor:
                                                    gdal.Warp('/vsimem/' + all_band_file_name + '.TIF', ds_temp,
                                                              xRes=10, yRes=10, dstSRS=self.main_coordinate_system, cutlineDSName=self.ROI,
                                                              outputBounds=band_output_limit, outputType=gdal.GDT_UInt16,
                                                              dstNodata=65535)
                                                else:
                                                    gdal.Warp('/vsimem/' + all_band_file_name + '.TIF', ds_temp,
                                                              xRes=10, yRes=10, dstSRS=self.main_coordinate_system,
                                                              outputBounds=band_output_limit, outputType=gdal.GDT_UInt16,
                                                              dstNodata=65535)
                                                time1 = time.time() - t1
                                                t2 = time.time()
                                                if self._cloud_removal_para:
                                                    qi_file_path = f'{qi_path}{str(sensing_date)}_{str(tile_num)}_QI.TIF'
                                                    if not os.path.exists(qi_file_path):
                                                        self.subset_tiffiles(['QI'], tiffile_serial_num, **kwargs)
                                                    self._qi_remove_cloud('/vsimem/' + all_band_file_name + '.TIF',
                                                                    qi_file_path, tiffile_serial_num, dst_nodata=65535,
                                                                    sparse_matrix_factor=self._sparsify_matrix_factor,
                                                                    **kwargs)
                                                gdal.Translate(subset_output_path + all_band_file_name + '.TIF',
                                                               '/vsimem/' + all_band_file_name + '.TIF', options=topts,
                                                               noData=65535)

                                                if self._combine_band_factor and band_output in combine_index_list:
                                                    temp_ds = gdal.Open(subset_output_path + all_band_file_name + '.TIF')
                                                    temp_array = temp_ds.GetRasterBand(1).ReadAsArray()
                                                    temp_array = temp_array.astype(np.float)
                                                    temp_array[temp_array == 65535] = np.nan
                                                    combine_index_array_list[combine_index_list.index(band_output)] = temp_array

                                                gdal.Unlink('/vsimem/' + all_band_file_name + '.TIF')
                                                time2 = time.time() - t2
                                                print(f'Subset {file_name} of consuming {str(time1)}, remove cloud consuming {str(time2)}!')
                                            except:
                                                self._subset_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                                                print(f'The {index} of {str(sensing_date)}_{str(tile_num)} is not valid')
                                                return

                                elif os.path.exists(subset_output_path + all_band_file_name + '.TIF') and self._combine_band_factor and band_output in combine_index_list:
                                    temp_ds = gdal.Open(subset_output_path + all_band_file_name + '.TIF')
                                    temp_array = temp_ds.GetRasterBand(1).ReadAsArray()
                                    temp_array = temp_array.astype(np.float)
                                    temp_array[temp_array == 65535] = np.nan
                                    combine_index_array_list[combine_index_list.index(band_output)] = temp_array

                            else:
                                if not os.path.exists(f'{subset_output_path}\\{str(sensing_date)}_{str(tile_num)}_B2.TIF'):
                                    print('Code error for B2!')
                                    sys.exit(-1)
                                else:
                                    if False in [os.path.exists(
                                        subset_output_path + str(sensing_date) + '_' + str(tile_num) + '_' + str(
                                            band_temp) + '.TIF') for band_temp in band_output_list]:
                                        if self._cloud_removal_para:
                                            qi_file_path = f'{qi_path}{str(sensing_date)}_{str(tile_num)}_QI.TIF'
                                            if not os.path.exists(qi_file_path):
                                                self.subset_tiffiles(['QI'], tiffile_serial_num, **kwargs)
                                            self._qi_remove_cloud(
                                                f'{subset_output_path}\\{str(sensing_date)}_{str(tile_num)}_B2.TIF',
                                                qi_file_path, tiffile_serial_num, dst_nodata=65535,
                                                sparse_matrix_factor=self._sparsify_matrix_factor, **kwargs)

                                    elif self._combine_band_factor and 'B2' in combine_index_list:
                                        temp_ds = gdal.Open(f'{subset_output_path}\\{str(sensing_date)}_{str(tile_num)}_B2.TIF')
                                        temp_array = temp_ds.GetRasterBand(1).ReadAsArray()
                                        temp_array = temp_array.astype(np.float)
                                        temp_array[temp_array == 65535] = np.nan
                                        combine_index_array_list[combine_index_list.index('B2')] = temp_array

                    if index == 'RGB':
                        gamma_coef = 1.5
                        if self._vi_clip_factor:
                            rgb_output_path = f'{self.output_path}Sentinel2_{self.ROI_name}_index\\RGB\\'
                        else:
                            rgb_output_path = f'{self.output_path}Sentinel2_constructed_index_index\\RGB\\'
                        bf.create_folder(rgb_output_path)

                        if not os.path.exists(f'{rgb_output_path}{str(sensing_date)}_{str(tile_num)}_RGB.tif') or overwritten_para:
                            b2_file = bf.file_filter(subset_output_path, containing_word_list=[f'{str(sensing_date)}_{str(tile_num)}_B2'])
                            b3_file = bf.file_filter(subset_output_path, containing_word_list=[f'{str(sensing_date)}_{str(tile_num)}_B3'])
                            b4_file = bf.file_filter(subset_output_path, containing_word_list=[f'{str(sensing_date)}_{str(tile_num)}_B4'])
                            b2_ds = gdal.Open(b2_file[0])
                            b3_ds = gdal.Open(b3_file[0])
                            b4_ds = gdal.Open(b4_file[0])
                            b2_array = b2_ds.GetRasterBand(1).ReadAsArray()
                            b3_array = b3_ds.GetRasterBand(1).ReadAsArray()
                            b4_array = b4_ds.GetRasterBand(1).ReadAsArray()

                            b2_array = ((b2_array / 10000) ** (1/gamma_coef) * 255).astype(np.int)
                            b3_array = ((b3_array / 10000) ** (1/gamma_coef) * 255).astype(np.int)
                            b4_array = ((b4_array / 10000) ** (1/gamma_coef) * 255).astype(np.int)

                            b2_array[b2_array > 255] = 0
                            b3_array[b3_array > 255] = 0
                            b4_array[b4_array > 255] = 0

                            dst_ds = gdal.GetDriverByName('GTiff').Create(f'{rgb_output_path}{str(sensing_date)}_{str(tile_num)}_RGB.tif', xsize=b2_array.shape[1], ysize=b2_array.shape[0], bands=3, eType=gdal.GDT_Byte, options=['COMPRESS=LZW', 'PREDICTOR=2'])
                            dst_ds.SetGeoTransform(b2_ds.GetGeoTransform())  # specify coords
                            dst_ds.SetProjection(b2_ds.GetProjection())  # export coords to file
                            dst_ds.GetRasterBand(1).WriteArray(b4_array)
                            dst_ds.GetRasterBand(1).SetNoDataValue(0)# write r-band to the raster
                            dst_ds.GetRasterBand(2).WriteArray(b3_array)
                            dst_ds.GetRasterBand(2).SetNoDataValue(0) # write g-band to the raster
                            dst_ds.GetRasterBand(3).WriteArray(b2_array)
                            dst_ds.GetRasterBand(3).SetNoDataValue(0)# write b-band to the raster
                            dst_ds.FlushCache()  # write to disk
                            dst_ds = None

                elif (not overwritten_para and not os.path.exists(subset_output_path + file_name + '.TIF') and not (index == 'QI' or index == 'all_band' or index == '4visual' or index in self.band_output_list)) or self._combine_band_factor:
                    if index == 'NDVI':
                        # time1 = time.time()
                        ds_list = self._check_output_band_statue(['B8', 'B4'], tiffile_serial_num, **kwargs)
                        # print('process b8 and b4' + str(time.time() - time1))
                        if ds_list is not None:
                            if self._sparsify_matrix_factor:
                                B8_array = sp.csr_matrix(ds_list[0].GetRasterBand(1).ReadAsArray()).astype(np.float)
                                B4_array = sp.csr_matrix(ds_list[1].GetRasterBand(1).ReadAsArray()).astype(np.float)
                            else:
                                B8_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                                B4_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            # print(time.time()-time1)
                            output_array = (B8_array - B4_array) / (B8_array + B4_array)
                            B4_array = None
                            B8_array = None
                            # print(time.time()-time1)
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'MNDWI':
                        ds_list = self._check_output_band_statue(['B3', 'B11'], tiffile_serial_num, **kwargs)
                        if ds_list is not None:
                            if self._sparsify_matrix_factor:
                                B3_array = sp.csr_matrix(ds_list[0].GetRasterBand(1).ReadAsArray()).astype(np.float)
                                B11_array = sp.csr_matrix(ds_list[1].GetRasterBand(1).ReadAsArray()).astype(np.float)
                            else:
                                B3_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                                B11_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = (B3_array - B11_array) / (B3_array + B11_array)
                            B3_array = None
                            B11_array = None
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'EVI':
                        ds_list = self._check_output_band_statue(['B2', 'B4', 'B8'], tiffile_serial_num, **kwargs)
                        if ds_list is not None:
                            B2_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                            B4_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            B8_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = 2.5 * (B8_array - B4_array) / (B8_array + 6 * B4_array - 7.5 * B2_array + 1)
                            B4_array = None
                            B8_array = None
                            B2_array = None
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'EVI2':
                        ds_list = self._check_output_band_statue(['B8', 'B4'], tiffile_serial_num, **kwargs)
                        # print('process b8 and b4' + str(time.time() - time1))
                        if ds_list is not None:
                            B8_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                            B4_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = 2.5 * (B8_array - B4_array) / (B8_array + 2.4 * B4_array + 1)
                            B4_array = None
                            B8_array = None
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'GNDVI':
                        ds_list = self._check_output_band_statue(['B8', 'B3'], tiffile_serial_num, **kwargs)
                        # print('process b8 and b4' + str(time.time() - time1))
                        if ds_list is not None:
                            if self._sparsify_matrix_factor:
                                B8_array = sp.csr_matrix(ds_list[0].GetRasterBand(1).ReadAsArray()).astype(np.float)
                                B3_array = sp.csr_matrix(ds_list[1].GetRasterBand(1).ReadAsArray()).astype(np.float)
                            else:
                                B8_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                                B3_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = (B8_array - B3_array) / (B8_array + B3_array)
                            B3_array = None
                            B8_array = None
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'NDVI_RE':
                        ds_list = self._check_output_band_statue(['B7', 'B5'], tiffile_serial_num, **kwargs)
                        # print('process b8 and b4' + str(time.time() - time1))
                        if ds_list is not None:
                            if self._sparsify_matrix_factor:
                                B7_array = sp.csr_matrix(ds_list[0].GetRasterBand(1).ReadAsArray()).astype(np.float)
                                B5_array = sp.csr_matrix(ds_list[1].GetRasterBand(1).ReadAsArray()).astype(np.float)
                            else:
                                B7_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                                B5_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = (B7_array - B5_array) / (B7_array + B5_array)
                            B5_array = None
                            B7_array = None
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'NDVI_RE2':
                        ds_list = self._check_output_band_statue(['B8', 'B5'], tiffile_serial_num, **kwargs)
                        # print('process b8 and b4' + str(time.time() - time1))
                        if ds_list is not None:
                            if self._sparsify_matrix_factor:
                                B8_array = sp.csr_matrix(ds_list[0].GetRasterBand(1).ReadAsArray()).astype(np.float)
                                B5_array = sp.csr_matrix(ds_list[1].GetRasterBand(1).ReadAsArray()).astype(np.float)
                            else:
                                B8_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                                B5_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = (B8_array - B5_array) / (B8_array + B5_array)
                            B5_array = None
                            B8_array = None
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'OSAVI':
                        ds_list = self._check_output_band_statue(['B8', 'B4'], tiffile_serial_num, **kwargs)
                        # print('Process B8 and B4 in' + str(time.time() - time1))
                        if ds_list is not None:
                            time1 = time.time()
                            B8_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                            B4_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = 1.16 * (B8_array - B4_array) / (B8_array + B4_array + 0.16)
                            B4_array = None
                            B8_array = None
                            # print(time.time()-time1)
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    elif index == 'IEI':
                        ds_list = self._check_output_band_statue(['B8', 'B4'], tiffile_serial_num, **kwargs)
                        # print('Process B8 and B4 in' + str(time.time() - time1))
                        if ds_list is not None:
                            time1 = time.time()
                            B8_array = ds_list[0].GetRasterBand(1).ReadAsArray().astype(np.float)
                            B4_array = ds_list[1].GetRasterBand(1).ReadAsArray().astype(np.float)
                            output_array = 1.5 * (B8_array - B4_array) / (B8_array + B4_array + 0.5)
                            B4_array = None
                            B8_array = None
                        else:
                            self._index_construction_failure_file.append([index, tiffile_serial_num, sensing_date, tile_num])
                            break
                    else:
                        print(f'{index} is not supported!')
                        sys.exit(-1)

                    if self._combine_band_factor and os.path.exists(subset_output_path + file_name + '.TIF'):
                        combine_index_array_list[combine_index_list.index(index)] = copy.copy(output_array)

                    if not os.path.exists(subset_output_path + file_name + '.TIF'):
                        # Output the VI
                        # output_array[np.logical_or(output_array > 1, output_array < -1)] = np.nan
                        if self._size_control_factor is True:
                            output_array[np.isnan(output_array)] = -3.2768
                            output_array = output_array * 10000
                            write_raster(ds_list[0], output_array, '/vsimem/', file_name + '.TIF', raster_datatype=gdal.GDT_Int16)
                            data_type = gdal.GDT_Int16
                        else:
                            write_raster(ds_list[0], output_array, '/vsimem/', file_name + '.TIF', raster_datatype=gdal.GDT_Float32)
                            data_type = gdal.GDT_Float32

                        if self._vi_clip_factor:
                            gdal.Warp('/vsimem/' + file_name + '2.TIF', '/vsimem/' + file_name + '.TIF', xRes=10, yRes=10, cutlineDSName=self.ROI, cropToCutline=True, outputType=data_type)
                        else:
                            gdal.Warp('/vsimem/' + file_name + '2.TIF', '/vsimem/' + file_name + '.TIF', xRes=10, yRes=10, outputType=data_type)
                        gdal.Translate(subset_output_path + file_name + '.TIF', '/vsimem/' + file_name + '2.TIF', options=topts)
                        gdal.Unlink('/vsimem/' + file_name + '.TIF')
                        gdal.Unlink('/vsimem/' + file_name + '2.TIF')

                print(f'Finish processing {index} data in {str(time.time() - start_temp)}s ({str(tiffile_serial_num + 1)} of {str(self.S2_metadata_size)})')

                # Generate SA map
                if not os.path.exists(self.output_path + 'ROI_map\\' + self.ROI_name + '_map.npy'):
                    if self._vi_clip_factor:
                        file_list = bf.file_filter(f'{self.output_path}Sentinel2_{self.ROI_name}_index\\all_band\\', ['.TIF'], and_or_factor='and')
                    else:
                        file_list = bf.file_filter(f'{self.output_path}Sentinel2_constructed_index\\all_band\\', ['.TIF'], and_or_factor='and')
                    bf.create_folder(self.output_path + 'ROI_map\\')
                    ds_temp = gdal.Open(file_list[0])
                    array_temp = ds_temp.GetRasterBand(1).ReadAsArray()
                    array_temp[:, :] = 1
                    write_raster(ds_temp, array_temp, self.cache_folder, 'temp_' + self.ROI_name + '.TIF',
                                 raster_datatype=gdal.GDT_Int16)
                    if retrieve_srs(ds_temp) != self.main_coordinate_system:
                        gdal.Warp('/vsimem/' + 'ROI_map\\' + self.ROI_name + '_map.TIF',
                                  self.cache_folder + 'temp_' + self.ROI_name + '.TIF',
                                  dstSRS=self.main_coordinate_system, cutlineDSName=self.ROI, cropToCutline=True,
                                  xRes=30, yRes=30, dstNodata=-32768)
                    else:
                        gdal.Warp('/vsimem/' + 'ROI_map\\' + self.ROI_name + '_map.TIF',
                                  self.cache_folder + 'temp_' + self.ROI_name + '.TIF', cutlineDSName=self.ROI,
                                  cropToCutline=True, dstNodata=-32768, xRes=30, yRes=30)
                    ds_sa_temp = gdal.Open('/vsimem/' + 'ROI_map\\' + self.ROI_name + '_map.TIF')
                    ds_sa_array = ds_sa_temp.GetRasterBand(1).ReadAsArray()
                    if (ds_sa_array == -32768).all() == False:
                        np.save(self.output_path + 'ROI_map\\' + self.ROI_name + '_map.npy', ds_sa_array)
                        if retrieve_srs(ds_temp) != self.main_coordinate_system:
                            gdal.Warp(self.output_path + 'ROI_map\\' + self.ROI_name + '_map.TIF',
                                      self.cache_folder + 'temp_' + self.ROI_name + '.TIF',
                                      dstSRS=self.main_coordinate_system, cutlineDSName=self.ROI, cropToCutline=True,
                                      xRes=30, yRes=30, dstNodata=-32768)
                        else:
                            gdal.Warp(self.output_path + 'ROI_map\\' + self.ROI_name + '_map.TIF',
                                      self.cache_folder + 'temp_' + self.ROI_name + '.TIF', cutlineDSName=self.ROI,
                                      cropToCutline=True, dstNodata=-32768, xRes=30, yRes=30)
                    gdal.Unlink('/vsimem/' + 'ROI_map\\' + self.ROI_name + '_map.TIF')
                    ds_temp = None
                    ds_sa_temp = None
                    remove_all_file_and_folder(bf.file_filter(self.cache_folder, ['temp', '.TIF'], and_or_factor='and'))

            if self._combine_band_factor:
                if False in [type(combine_array_temp) == np.ndarray for combine_array_temp in combine_index_array_list]:
                    raise Exception('Code issue during combine band factor!')
                elif False in [combine_array_temp.shape == combine_index_array_list[0].shape for combine_array_temp in combine_index_array_list]:
                    raise Exception('Consistency issue during combine band factor!')
                else:
                    dst_ds = gdal.GetDriverByName('GTiff').Create(f'{combine_band_folder}{str(sensing_date)}_{str(tile_num)}.TIF', xsize=combine_index_array_list[0].shape[1],
                        ysize=combine_index_array_list[0].shape[0], bands=len(combine_index_array_list), eType=gdal.GDT_Float32, options=['COMPRESS=LZW', 'PREDICTOR=2'])

                    if self._vi_clip_factor:
                        file_list = bf.file_filter(f'{self.output_path}Sentinel2_{self.ROI_name}_index\\all_band\\', ['.TIF'], and_or_factor='and')
                    else:
                        file_list = bf.file_filter(f'{self.output_path}Sentinel2_constructed_index\\all_band\\', ['.TIF'], and_or_factor='and')
                    ds_temp = gdal.Open(file_list[0])

                    dst_ds.SetGeoTransform(ds_temp.GetGeoTransform())  # specify coords
                    dst_ds.SetProjection(ds_temp.GetProjection())  # export coords to file
                    for len_t in range(1, len(combine_index_array_list) + 1):
                        dst_ds.GetRasterBand(len_t).WriteArray(combine_index_array_list[len_t - 1])
                        dst_ds.GetRasterBand(len_t).SetNoDataValue(np.nan)
                    dst_ds.FlushCache()  # write to disk
                    dst_ds = None
        else:
            print('Caution! the input variable VI_list should be a list and make sure all of them are in Capital Letter')
            sys.exit(-1)
        return

    def create_index_cube(self, band_list):
        # This function is used to construct index cube for deep learning and image identification
        supported_band_list = ['NDVI', 'MNDWI', 'EVI', 'EVI2', 'OSAVI', 'GNDVI',
                               'NDVI_RE', 'NDVI_RE2', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7', 'B8', 'B9',
                               'B11', 'B12']


    def check_subset_intergrality(self, indicator, **kwargs):
        if self.ROI_name is None and ('ROI' not in kwargs.keys() and 'ROI_name' not in kwargs.keys()):
            check_path = f'{self.output_path}{str(indicator)}\\'
        elif self.ROI_name is None and ('ROI' in kwargs.keys() or 'ROI_name' in kwargs.keys()):
            self._subset_indicator_process(**kwargs)
            if self.ROI_name is None:
                print()

    def temporal_mosaic(self, indicator, date, **kwargs):
        self._check_metadata_availability()
        self.check_subset_intergrality(indicator)
        pass

    def _process_2dc_para(self, **kwargs):
        # Detect whether all the indicators are valid
        for kwarg_indicator in kwargs.keys():
            if kwarg_indicator not in ('inherit_from_logfile', 'ROI', 'ROI_name', 'dc_overwritten_para', 'remove_nan_layer', 'manually_remove_datelist'):
                raise NameError(f'{kwarg_indicator} is not supported kwargs! Please double check!')

        # process clipped_overwritten_para
        if 'dc_overwritten_para' in kwargs.keys():
            if type(kwargs['dc_overwritten_para']) is bool:
                self._dc_overwritten_para = kwargs['dc_overwritten_para']
            else:
                raise TypeError('Please mention the dc_overwritten_para should be bool type!')
        else:
            self._clipped_overwritten_para = False

        # process inherit from logfile
        if 'inherit_from_logfile' in kwargs.keys():
            if type(kwargs['inherit_from_logfile']) is bool:
                self._inherit_from_logfile = kwargs['inherit_from_logfile']
            else:
                raise TypeError('Please mention the dc_overwritten_para should be bool type!')
        else:
            self._inherit_from_logfile = False

        # process remove_nan_layer
        if 'remove_nan_layer' in kwargs.keys():
            if type(kwargs['remove_nan_layer']) is bool:
                self._remove_nan_layer = kwargs['remove_nan_layer']
            else:
                raise TypeError('Please mention the remove_nan_layer should be bool type!')
        else:
            self._remove_nan_layer = False

        # process remove_nan_layer
        if 'manually_remove_datelist' in kwargs.keys():
            if type(kwargs['manually_remove_datelist']) is list:
                self._manually_remove_datelist = kwargs['manually_remove_datelist']
                self._manually_remove_para = True
            else:
                raise TypeError('Please mention the manually_remove_datelist should be list type!')
        else:
            self._manually_remove_datelist = False
            self._manually_remove_para = False

        # process ROI_NAME
        if 'ROI_name' in kwargs.keys():
            self.ROI_name = kwargs['ROI_name']
        elif self.ROI_name is None and self._inherit_from_logfile:
            self._retrieve_para(['ROI_name'])
        elif self.ROI_name is None:
            raise Exception('Notice the ROI name was missed!')

        # process ROI
        if 'ROI' in kwargs.keys():
            self.ROI = kwargs['ROI']
        elif self.ROI is None and self._inherit_from_logfile:
            self._retrieve_para(['ROI'])
        elif self.ROI is None:
            raise Exception('Notice the ROI was missed!')

        # Retrieve size control factor
        self._retrieve_para(['size_control_factor'])

    def to_datacube(self, VI_list, *args, **kwargs):

        # for the MP
        if args != () and type(args[0]) == dict:
            kwargs = copy.copy(args[0])

        # process clip parameter
        self._process_2dc_para(**kwargs)

        # generate dc_vi
        for VI in VI_list:
            # Remove all files which not meet the requirements
            if self.ROI_name is None:
                self.dc_vi[VI + 'input_path'] = self.output_path + f'Sentinel2_constructed_index\\{VI}\\'
            else:
                self.dc_vi[VI + 'input_path'] = self.output_path + f'Sentinel2_{self.ROI_name}_index\\{VI}\\'

            # path check
            if not os.path.exists(self.dc_vi[VI + 'input_path']):
                raise Exception('Please validate the roi name and vi for datacube output!')

            if self.ROI_name is None:
                self.dc_vi[VI] = self.output_path + 'Sentinel2_constructed_datacube\\' + VI + '_datacube\\'
            else:
                self.dc_vi[VI] = self.output_path + 'Sentinel2_' + self.ROI_name + '_datacube\\' + VI + '_datacube\\'
            bf.create_folder(self.dc_vi[VI])

            if len(bf.file_filter(self.dc_vi[VI + 'input_path'], [VI, '.TIF'], and_or_factor='and')) != self.S2_metadata_size:
                raise ValueError(f'{VI} of the {self.ROI_name} is not consistent')

        for VI in VI_list:
            if self._dc_overwritten_para or not os.path.exists(self.dc_vi[VI] + VI + '_datacube.npy') or not os.path.exists(self.dc_vi[VI] + 'date.npy') or not os.path.exists(self.dc_vi[VI] + 'header.npy'):

                if self.ROI_name is None:
                    print('Start processing ' + VI + ' datacube.')
                    header_dic = {'ROI_name': None, 'VI': VI, 'Datatype': 'float', 'ROI': None, 'Study_area': None, 'sdc_factor': False, 'coordinate_system': self.main_coordinate_system, 'oritif_folder': self.dc_vi[VI + 'input_path']}
                else:
                    print('Start processing ' + VI + ' datacube of the ' + self.ROI_name + '.')
                    sa_map = np.load(bf.file_filter(self.output_path + 'ROI_map\\', [self.ROI_name, '.npy'], and_or_factor='and')[0], allow_pickle=True)
                    header_dic = {'ROI_name': self.ROI_name, 'VI': VI, 'Datatype': 'float', 'ROI': self.ROI, 'Study_area': sa_map, 'sdc_factor': False, 'coordinate_system': self.main_coordinate_system, 'oritif_folder': self.dc_vi[VI + 'input_path']}

                start_time = time.time()
                VI_stack_list = bf.file_filter(self.dc_vi[VI + 'input_path'], [VI, '.TIF'])
                VI_stack_list.sort()
                temp_ds = gdal.Open(VI_stack_list[0])
                cols, rows = temp_ds.RasterXSize, temp_ds.RasterYSize
                data_cube_temp = np.zeros((rows, cols, len(VI_stack_list)), dtype=np.float16)
                date_cube_temp = []
                header_dic['ds_file'] = VI_stack_list[0]

                i = 0
                while i < len(VI_stack_list):
                    date_cube_temp[i] = int(VI_stack_list[i][VI_stack_list[i].find(VI + '\\') + 1 + len(VI): VI_stack_list[i].find(VI + '\\') + 9 + len(VI)])
                    i += 1

                nodata_value = np.nan
                i = 0
                while i < len(VI_stack_list):
                    temp_ds2 = gdal.Open(VI_stack_list[i])
                    temp_band = temp_ds2.GetRasterBand(1)
                    if i != 0 and nodata_value != temp_band.GetNoDataValue():
                        raise Exception(f"The nodata value for the {VI} file list is not consistent!")
                    else:
                        nodata_value = temp_band.GetNoDataValue()
                    temp_raster = temp_ds2.GetRasterBand(1).ReadAsArray()
                    data_cube_temp[:, :, i] = temp_raster
                    i += 1

                if self._size_control_factor:
                    data_cube_temp[data_cube_temp == -32768] = np.nan
                    data_cube_temp = data_cube_temp / 10000

                if self._manually_remove_para is True and self._manually_remove_datelist is not None:
                    i_temp = 0
                    manual_remove_date_list_temp = copy.copy(self._manually_remove_datelist)
                    while i_temp < date_cube_temp.shape[0]:
                        if str(date_cube_temp[i_temp]) in manual_remove_date_list_temp:
                            manual_remove_date_list_temp.remove(str(date_cube_temp[i_temp]))
                            date_cube_temp = np.delete(date_cube_temp, i_temp, 0)
                            data_cube_temp = np.delete(data_cube_temp, i_temp, 2)
                            i_temp -= 1
                        i_temp += 1

                    if manual_remove_date_list_temp:
                        raise Exception('Some manual input date is not properly removed')

                elif self._manually_remove_para is True and self._manually_remove_datelist is None:
                    raise ValueError('Please correctly input the manual input datelist')

                if self._remove_nan_layer:
                    i_temp = 0
                    while i_temp < date_cube_temp.shape[0]:
                        if np.isnan(data_cube_temp[:, :, i_temp]).all() == True or (data_cube_temp[:, :, i_temp] == nodata_value).all() == True:
                            date_cube_temp = np.delete(date_cube_temp, i_temp, 0)
                            data_cube_temp = np.delete(data_cube_temp, i_temp, 2)
                            i_temp -= 1
                        i_temp += 1
                print('Finished in ' + str(time.time() - start_time) + ' s.')

                # Write the datacube
                print('Start writing the ' + VI + ' datacube.')
                start_time = time.time()
                np.save(self.dc_vi[VI] + 'header.npy', header_dic)
                np.save(self.dc_vi[VI] + 'date.npy', date_cube_temp.astype(np.uint32).tolist())
                np.save(self.dc_vi[VI] + str(VI) + '_datacube.npy', data_cube_temp.astype(np.float16))
                end_time = time.time()
                print('Finished writing ' + VI + ' datacube in ' + str(end_time - start_time) + ' s.')


class Sentinel2_dc(object):
    def __init__(self, dc_filepath, work_env=None, sdc_factor=False):
        # define var
        if os.path.exists(dc_filepath) and os.path.isdir(dc_filepath):
            self.dc_filepath = dc_filepath
        else:
            raise ValueError('Please input a valid dc filepath')
        eliminating_all_not_required_file(self.dc_filepath, filename_extension=['npy'])

        # Define the sdc_factor:
        self.sdc_factor = False
        if type(sdc_factor) is bool:
            self.sdc_factor = sdc_factor
        else:
            raise TypeError('Please input the sdc factor as bool type!')

        # Read header
        header_file = bf.file_filter(self.dc_filepath, ['header.npy'])
        if len(header_file) == 0:
            raise ValueError('There has no valid dc or the header file of the dc was missing!')
        elif len(header_file) > 1:
            raise ValueError('There has more than one header file in the dir')
        else:
            try:
                self.dc_header = np.load(header_file[0], allow_pickle=True).item()
                if type(self.dc_header) is not dict:
                    raise Exception('Please make sure the header file is a dictionary constructed in python!')

                for dic_name in ['ROI_name', 'VI', 'Datatype', 'ROI', 'Study_area', 'ds_file', 'sdc_factor', 'coordinate_system', 'oritif_folder']:
                    if dic_name not in self.dc_header.keys():
                        raise Exception(f'The {dic_name} is not in the dc header, double check!')
                    else:
                        if dic_name == 'Study_area':
                            self.__dict__['sa_map'] = self.dc_header[dic_name]
                        else:
                            self.__dict__[dic_name] = self.dc_header[dic_name]
            except:
                raise Exception('Something went wrong when reading the header!')

        # Read doy or date file of the Datacube
        try:
            if self.sdc_factor is True:
                # Read doylist
                if self.ROI_name is None:
                    doy_file = bf.file_filter(self.dc_filepath, ['doy.npy', str(self.VI)], and_or_factor='and')
                else:
                    doy_file = bf.file_filter(self.dc_filepath, ['doy.npy', str(self.VI), str(self.ROI_name)],
                                           and_or_factor='and')

                if len(doy_file) == 0:
                    raise ValueError('There has no valid doy file or file was missing!')
                elif len(doy_file) > 1:
                    raise ValueError('There has more than one doy file in the dc dir')
                else:
                    self.sdc_doylist = np.load(doy_file[0], allow_pickle=True)

            else:
                # Read datelist
                if self.ROI_name is None:
                    date_file = bf.file_filter(self.dc_filepath, ['date.npy', str(self.VI)], and_or_factor='and')
                else:
                    date_file = bf.file_filter(self.dc_filepath, ['date.npy', str(self.VI), str(self.ROI_name)],
                                            and_or_factor='and')

                if len(date_file) == 0:
                    raise ValueError('There has no valid dc or the date file of the dc was missing!')
                elif len(date_file) > 1:
                    raise ValueError('There has more than one date file in the dc dir')
                else:
                    self.dc_datelist = np.load(date_file[0], allow_pickle=True)

                # Define var for sequenced_dc
                self.sdc_output_folder = None
                self.sdc_doylist = []
                self.sdc_overwritten_para = False
        except:
            raise Exception('Something went wrong when reading the doy and date list!')

        # Read datacube
        try:
            if self.ROI_name is None:
                self.dc_filename = bf.file_filter(self.dc_filepath, ['datacube.npy', str(self.VI)], and_or_factor='and')
            else:
                self.dc_filename = bf.file_filter(self.dc_filepath, ['datacube.npy', str(self.VI), str(self.ROI_name)],
                                               and_or_factor='and')

            if len(self.dc_filename) == 0:
                raise ValueError('There has no valid dc or the dc was missing!')
            elif len(self.dc_filename) > 1:
                raise ValueError('There has more than one date file in the dc dir')
            else:
                self.dc = np.load(self.dc_filename[0], allow_pickle=True)
        except:
            raise Exception('Something went wrong when reading the datacube!')

        self.dc_XSize = self.dc.shape[0]
        self.dc_YSize = self.dc.shape[1]
        self.dc_ZSize = self.dc.shape[2]

        # Check work env
        if work_env is not None:
            self.work_env = Path(work_env).path_name
        else:
            self.work_env = Path(os.path.dirname(os.path.dirname(self.dc_filepath))).path_name
        self.root_path = Path(os.path.dirname(os.path.dirname(self.work_env))).path_name

        # Inundation parameter process
        self._DSWE_threshold = None
        self._flood_month_list = None
        self.flood_mapping_method = []

    def to_sdc(self, sdc_substitued=False, **kwargs):
        # Sequenced check
        if self.sdc_factor is True:
            raise Exception('The datacube has been already sequenced!')

        self.sdc_output_folder = self.work_env + self.VI + '_sequenced_datacube\\'
        bf.create_folder(self.sdc_output_folder)
        if self.sdc_overwritten_para or not os.path.exists(
                self.sdc_output_folder + 'header.npy') or not os.path.exists(
                self.sdc_output_folder + 'doy_list.npy') or not os.path.exists(
                self.sdc_output_folder + self.VI + '_sequenced_datacube.npy'):

            start_time = time.time()
            sdc_header = {'sdc_factor': True, 'VI': self.VI, 'ROI_name': self.ROI_name, 'Study_area': self.sa_map,
                          'original_dc_path': self.dc_filepath, 'original_datelist': self.dc_datelist,
                          'Datatype': self.Datatype, 'ds_file': self.ds_file, 'coordinate_system': self.coordinate_system, 'oritif_folder':self.oritif_folder}

            if self.ROI_name is not None:
                print('Start constructing ' + self.VI + ' sequenced datacube of the ' + self.ROI_name + '.')
                sdc_header['ROI'] = self.dc_header['ROI']
            else:
                print('Start constructing ' + self.VI + ' sequenced datacube.')

            self.sdc_doylist = []
            if 'dc_datelist' in self.__dict__.keys() and self.dc_datelist != []:
                for date_temp in self.dc_datelist:
                    date_temp = int(date_temp)
                    if date_temp not in self.sdc_doylist:
                        self.sdc_doylist.append(date_temp)
            else:
                raise Exception('Something went wrong for the datacube initialisation!')

            self.sdc_doylist = bf.date2doy(self.sdc_doylist)
            self.sdc_doylist = np.sort(np.array(self.sdc_doylist))
            self.sdc_doylist = self.sdc_doylist.tolist()

            if len(self.sdc_doylist) != len(self.dc_datelist):
                data_cube_inorder = np.zeros((self.dc.shape[0], self.dc.shape[1], len(self.sdc_doylist)), dtype=np.float)
                if self.dc.shape[2] == len(self.dc_datelist):
                    for doy_temp in self.sdc_doylist:
                        date_all = [z for z in range(self.dc_datelist) if self.dc_datelist[z] == bf.doy2date(doy_temp)]
                        if len(date_all) == 1:
                            data_cube_temp = self.dc[:, :, date_all[0]]
                            data_cube_temp[np.logical_or(data_cube_temp < -1, data_cube_temp > 1)] = np.nan
                            data_cube_temp = data_cube_temp.reshape(data_cube_temp.shape[0], -1)
                            data_cube_inorder[:, :, self.sdc_doylist.index(doy_temp)] = data_cube_temp
                        elif len(date_all) > 1:
                            if date_all[-1] - date_all[0] + 1 == len(date_all):
                                data_cube_temp = self.dc[:, :, date_all[0]: date_all[-1]]
                            else:
                                print('date long error')
                                sys.exit(-1)
                            data_cube_temp_temp = np.nanmax(data_cube_temp, axis=2)
                            data_cube_inorder[:, :, self.sdc_doylist.index(doy_temp)] = data_cube_temp_temp
                        else:
                            print('Something error during generate sequenced datecube')
                            sys.exit(-1)
                    np.save(f'{self.sdc_output_folder}{self.VI}_sequenced_datacube.npy', data_cube_inorder)
                else:
                    raise Exception('Consistency error!')
            elif len(self.sdc_doylist) == len(self.dc_datelist):
                shutil.copyfile(self.dc_filename[0], f'{self.sdc_output_folder}{self.VI}_sequenced_datacube.npy')
            else:
                raise Exception('Code error!')

            np.save(f'{self.sdc_output_folder}header.npy', sdc_header)
            np.save(f'{self.sdc_output_folder}doy.npy', self.sdc_doylist)

            if self.ROI_name is not None:
                print(self.VI + ' sequenced datacube of the ' + self.ROI_name + ' was constructed using ' + str(
                    time.time() - start_time) + ' s.')
            else:
                print(
                    self.VI + ' sequenced datacube was constructed using ' + str(time.time() - start_time) + ' s.')
        else:
            print(self.VI + ' sequenced datacube has already constructed!.')

        # Substitute sdc
        if type(sdc_substitued) is bool:
            if sdc_substitued is True:
                self.__init__(self.sdc_output_folder)
                return self
        else:
            raise TypeError('Please input the sdc_substitued as bool type factor!')


class Sentinel2_dcs(object):
    def __init__(self, *args, work_env=None, auto_harmonised=True):

        # Generate the datacubes list
        self.Sentinel2_dcs = []
        for args_temp in args:
            if type(args_temp) != Sentinel2_dc:
                raise TypeError('The Landsat datacubes was a bunch of Landsat datacube!')
            else:
                self.Sentinel2_dcs.append(args_temp)

        # Validation and consistency check
        if len(self.Sentinel2_dcs) == 0:
            raise ValueError('Please input at least one valid Landsat datacube')

        if type(auto_harmonised) != bool:
            raise TypeError('Please input the auto harmonised factor as bool type!')
        else:
            harmonised_factor = False

        self.index_list, ROI_list, ROI_name_list, Datatype_list, ds_list, study_area_list, sdc_factor_list, doy_list, coordinate_system_list, oritif_folder_list = [], [], [], [], [], [], [], [], [], []
        x_size, y_size, z_size = 0, 0, 0
        for dc_temp in self.Sentinel2_dcs:
            if x_size == 0 and y_size == 0 and z_size == 0:
                x_size, y_size, z_size = dc_temp.dc_XSize, dc_temp.dc_YSize, dc_temp.dc_ZSize
            elif x_size != dc_temp.dc_XSize or y_size != dc_temp.dc_YSize:
                raise Exception('Please make sure all the datacube share the same size!')
            elif z_size != dc_temp.dc_ZSize:
                if auto_harmonised:
                    harmonised_factor = True
                else:
                    raise Exception(
                        'The datacubes is not consistent in the date dimension! Turn auto harmonised fator as True if wanna avoid this problem!')

            self.index_list.append(dc_temp.VI)
            ROI_name_list.append(dc_temp.ROI_name)
            sdc_factor_list.append(dc_temp.sdc_factor)
            ROI_list.append(dc_temp.ROI)
            ds_list.append(dc_temp.ds_file)
            study_area_list.append(dc_temp.sa_map)
            Datatype_list.append(dc_temp.Datatype)
            coordinate_system_list.append(dc_temp.coordinate_system)
            oritif_folder_list.append(dc_temp.oritif_folder)

        if x_size != 0 and y_size != 0 and z_size != 0:
            self.dcs_XSize, self.dcs_YSize, self.dcs_ZSize = x_size, y_size, z_size
        else:
            raise Exception('Please make sure all the datacubes was not void')

        # Check the consistency of the roi list
        if len(ROI_list) == 0 or False in [len(ROI_list) == len(self.index_list),
                                           len(self.index_list) == len(sdc_factor_list),
                                           len(ROI_name_list) == len(sdc_factor_list),
                                           len(ROI_name_list) == len(ds_list), len(ds_list) == len(study_area_list),
                                           len(study_area_list) == len(Datatype_list),
                                           len(coordinate_system_list) == len(Datatype_list),
                                           len(oritif_folder_list) == len(coordinate_system_list)]:
            raise Exception('The ROI list or the index list for the datacubes were not properly generated!')
        elif False in [roi_temp == ROI_list[0] for roi_temp in ROI_list]:
            raise Exception('Please make sure all datacubes were in the same roi!')
        elif False in [sdc_temp == sdc_factor_list[0] for sdc_temp in sdc_factor_list]:
            raise Exception('Please make sure all dcs were consistent!')
        elif False in [roi_name_temp == ROI_name_list[0] for roi_name_temp in ROI_name_list]:
            raise Exception('Please make sure all dcs were consistent!')
        elif False in [(sa_temp == study_area_list[0]).all() for sa_temp in study_area_list]:
            raise Exception('Please make sure all dcs were consistent!')
        elif False in [dt_temp == Datatype_list[0] for dt_temp in Datatype_list]:
            raise Exception('Please make sure all dcs were consistent!')
        elif False in [coordinate_system_temp == coordinate_system_list[0] for coordinate_system_temp in coordinate_system_list]:
            raise Exception('Please make sure all coordinate system were consistent!')

        # Define the field
        self.ROI = ROI_list[0]
        self.ROI_name = ROI_name_list[0]
        self.sdc_factor = sdc_factor_list[0]
        self.Datatype = Datatype_list[0]
        self.sa_map = study_area_list[0]
        self.ds_file = ds_list[0]
        self.main_coordinate_system = coordinate_system_list[0]
        self.oritif_folder = oritif_folder_list

        # Read the doy or date list
        if self.sdc_factor is False:
            raise Exception('Please sequenced the datacubes before further process!')
        else:
            doy_list = [temp.sdc_doylist for temp in self.Sentinel2_dcs]
            if False in [temp.shape[0] == doy_list[0].shape[0] for temp in doy_list] or False in [
                (temp == doy_list[0]).all() for temp in doy_list]:
                if auto_harmonised:
                    harmonised_factor = True
                else:
                    raise Exception('The datacubes is not consistent in the date dimension! Turn auto harmonised factor as True if wanna avoid this problem!')
            else:
                self.doy_list = self.Sentinel2_dcs[0].sdc_doylist

        # Harmonised the dcs
        if harmonised_factor:
            self._auto_harmonised_dcs()

        #  Define the output_path
        if work_env is None:
            self.work_env = Path(os.path.dirname(os.path.dirname(self.Sentinel2_dcs[0].dc_filepath))).path_name
        else:
            self.work_env = work_env

        # Define var for the flood mapping
        self.inun_det_method_dic = {}
        self._variance_num = 2
        self._inundation_overwritten_factor = False
        self._DEM_path = None
        self._DT_std_fig_construction = False
        self._construct_inundated_dc = True
        self._flood_mapping_accuracy_evaluation_factor = False
        self.inundation_para_folder = self.work_env + '\\Landsat_Inundation_Condition\\Inundation_para\\'
        self._sample_rs_link_list = None
        self._sample_data_path = None
        self._flood_mapping_method = ['DSWE', 'DT', 'AWEI', 'rs_dem']
        bf.create_folder(self.inundation_para_folder)

        # Define var for the phenological analysis
        self._curve_fitting_algorithm = None
        self._flood_removal_method = None
        self._curve_fitting_dic = {}

        # Define var for NIPY reconstruction
        self._add_NIPY_dc = True
        self._NIPY_overwritten_factor = False

        # Define var for phenology metrics generation
        self._phenology_index_all = ['annual_ave_VI', 'flood_ave_VI', 'unflood_ave_VI', 'max_VI', 'max_VI_doy',
                                     'bloom_season_ave_VI', 'well_bloom_season_ave_VI']
        self._curve_fitting_dic = {}
        self._all_quantify_str = None

        # Define var for flood_free_phenology_metrics
        self._flood_free_pm = ['annual_max_VI', 'average_VI_between_max_and_flood']
    
    def _auto_harmonised_dcs(self):

        doy_all = np.array([])
        for dc_temp in self.Sentinel2_dcs:
            doy_all = np.concatenate([doy_all, dc_temp.sdc_doylist], axis=0)
        doy_all = np.sort(np.unique(doy_all))

        i = 0
        while i < len(self.Sentinel2_dcs):
            m_factor = False
            for doy in doy_all:
                if doy not in self.Sentinel2_dcs[i].sdc_doylist:
                    m_factor = True
                    self.Sentinel2_dcs[i].dc = np.insert(self.Sentinel2_dcs[i].dc, np.argwhere(doy_all == doy)[0], np.nan * np.zeros([self.Sentinel2_dcs[i].dc_XSize, self.Sentinel2_dcs[i].dc_YSize, 1]), axis=2)

            if m_factor:
                self.Sentinel2_dcs[i].sdc_doylist = copy.copy(doy_all)
                self.Sentinel2_dcs[i].dc_ZSize = self.Sentinel2_dcs[i].dc.shape[2]

            i += 1

        z_size, doy_list = 0, []
        for dc_temp in self.Sentinel2_dcs:
            if z_size == 0:
                z_size = dc_temp.dc_ZSize
            elif z_size != dc_temp.dc_ZSize:
                raise Exception('Auto harmonised failure!')
            doy_list.append(dc_temp.sdc_doylist)

        if False in [temp.shape[0] == doy_list[0].shape[0] for temp in doy_list] or False in [(temp == doy_list[0]).all() for temp in doy_list]:
            raise Exception('Auto harmonised failure!')

        self.dcs_ZSize = z_size
        self.doy_list = doy_list[0]

    def append(self, dc_temp: Sentinel2_dc) -> None:
        if type(dc_temp) is not Sentinel2_dc:
            raise TypeError('The appended data should be a Sentinel2_dc!')

        for indicator in ['ROI', 'ROI_name', 'sdc_factor', 'main_coordinate_system']:
            if dc_temp.__dict__[indicator] != self.__dict__[indicator]:
                raise ValueError('The appended datacube is not consistent with the original datacubes')

        if self.dcs_XSize != dc_temp.dc_XSize or self.dcs_YSize != dc_temp.dc_YSize or self.dcs_ZSize != dc_temp.dc_ZSize:
            raise ValueError('The appended datacube has different size compared to the original datacubes')

        if (self.doy_list != dc_temp.sdc_doylist).any():
            raise ValueError('The appended datacube has doy list compared to the original datacubes')

        self.index_list.append(dc_temp.VI)
        self.oritif_folder.append(dc_temp.oritif_folder)
        self.Sentinel2_dcs.append(dc_temp)

    def extend(self, dcs_temp) -> None:
        if type(dcs_temp) is not Sentinel2_dcs:
            raise TypeError('The appended data should be a Sentinel2_dcs!')

        for indicator in ['ROI', 'ROI_name', 'sdc_factor', 'dcs_XSize', 'dcs_YSize', 'dcs_ZSize', 'doy_list', 'main_coordinate_system']:
            if dcs_temp.__dict__[indicator] != self.__dict__[indicator]:
                raise ValueError('The appended datacube is not consistent with the original datacubes')

        self.index_list.extend(dcs_temp.index_list)
        self.oritif_folder.extend(dcs_temp.oritif_folder)
        self.Sentinel2_dcs.extend(dcs_temp)

    def inundation_detection(self):
        pass

    def generate_phenology_metric(self):
        pass

    def link_GEDI_S2_phenology_inform(self):
        pass

    def _process_link_GEDI_S2_para(self, **kwargs):
        # Detect whether all the indicators are valid
        for kwarg_indicator in kwargs.keys():
            if kwarg_indicator not in ('retrieval_method'):
                raise NameError(f'{kwarg_indicator} is not supported kwargs! Please double check!')

        # process clipped_overwritten_para
        if 'retrieval_method' in kwargs.keys():
            if type(kwargs['retrieval_method']) is str and kwargs['dc_overwritten_para'] in ['nearest_neighbor', 'linear_interpolation']:
                self._GEDI_link_S2_retrieval_method = kwargs['dc_overwritten_para']
            else:
                raise TypeError('Please mention the dc_overwritten_para should be str type!')
        else:
            self._GEDI_link_S2_retrieval_method = 'nearest_neighbor'

    def _extract_value2shpfile(self, shpfile, index, date):

        # Define vars
        ori_folder_temp = self.oritif_folder[self.index_list.index(date)]
        file_list = bf.file_filter(ori_folder_temp, containing_word_list=[str(date), str(index)])
        array_mosaic = []

        # Mosaic all tiffiles
        for file_temp in file_list:
            ds_temp = gdal.Open(file_temp)
            if array_mosaic == []:
                array_mosaic = ds_temp.GetRasterBand(1).ReadAsArray()
                array_mosaic = array_mosaic.astype(np.float)
                array_mosaic[array_mosaic == -32768] = np.nan
            else:
                array_temp = ds_temp.GetRasterBand(1).ReadAsArray()
                array_temp = array_temp.astype(np.float)
                array_temp[array_temp == -32768] = np.nan
                array_mosaic = np.nanmean(array_temp, array_mosaic)
        bf.write_raster(ds_temp, array_mosaic, '/vsimem/',
                        str(date) + '_' + str(index) + '.tif',
                        raster_datatype=gdal.GDT_Float32)

        # Extract the value to shpfile
        info_temp = zonal_stats(shpfile, f'/vsimem/{str(date)}_{str(index)}.tif',
                                stats=['count', 'min', 'max', 'sum'],
                                add_stats={'nanmean': no_nan_mean})

        gdal.Unlink('/vsimem/', str(date) + '_' + str(index) + '.tif')
        return info_temp

    def link_GEDI_S2_inform(self, GEDI_xlsx_file, index_list, **kwargs):

        # Two different method0 Nearest data and linear interpolation
        self._process_link_GEDI_S2_para()

        # Retrieve GEDI inform
        GEDI_list_temp = gedi.GEDI_list(GEDI_xlsx_file)

        # Retrieve the S2 inform
        for index_temp in index_list:

            if index_temp not in self.index_list:
                raise Exception(f'The {str(index_temp)} is not a valid index or is not inputted into the dcs!')

            if self._GEDI_link_S2_retrieval_method == 'nearest_neighbor':
                # Link GEDI and S2 inform using nearest data
                i = 0
                GEDI_list_temp.GEDI_df.insert(loc=len(GEDI_list_temp.GEDI_df.columns), column=f'S2_nearest_{index_temp}_value', value=np.nan)
                GEDI_list_temp.GEDI_df.insert(loc=len(GEDI_list_temp.GEDI_df.columns), column=f'S2_nearest_{index_temp}_date', value=np.nan)

                while i <= GEDI_list_temp.df_size:

                    # Get the basic inform of the i GEDI point
                    lat = GEDI_list_temp.GEDI_df['Latitude'][i]
                    lon = GEDI_list_temp.GEDI_df['Longitude'][i]
                    date_temp = GEDI_list_temp.GEDI_df['Date'][i]
                    year_temp = int(date_temp) // 1000

                    # Draw a circle around a point
                    central_point = shapely.geometry.Point([lat, lon])  # location
                    n_points = 360 * 10
                    diameter = 30  # meters
                    angles = np.linspace(0, 360, n_points)
                    polygon_coordinate = geog.propagate(central_point, angles, diameter)
                    polygon = shapely.geometry.mapping(shapely.geometry.Polygon(polygon_coordinate))

                    doy_list = self.doy_list

                    for date_range in range(0, 365):
                        para = False
                        indi_temp = None
                        if True in [dc_doy_temp in range(date_temp - date_range, date_temp + date_range + 1) for dc_doy_temp in doy_list]:
                            for dc_doy_temp in doy_list:
                                if dc_doy_temp in range(date_temp - date_range, date_temp + date_range + 1):
                                    ori_folder_temp = self.oritif_folder[self.index_list.index(index_temp)]
                                    file_list = bf.file_filter(ori_folder_temp, containing_word_list=[str(index_temp), str(dc_doy_temp)])
                                    array_mosaic = []
                                    for file_temp in file_list:
                                        ds_temp = gdal.Open(file_temp)
                                        if array_mosaic == []:
                                            array_mosaic = ds_temp.GetRasterBand(1).ReadAsArray()
                                            array_mosaic = array_mosaic.astype(np.float)
                                            array_mosaic[array_mosaic == -32768] = np.nan
                                        else:
                                            array_temp = ds_temp.GetRasterBand(1).ReadAsArray()
                                            array_temp = array_temp.astype(np.float)
                                            array_temp[array_temp == -32768] = np.nan
                                            array_mosaic = np.nanmean(array_temp, array_mosaic)
                                    bf.write_raster(ds_temp, array_mosaic, '/vsimem/', str(dc_doy_temp) + '_' + str(index_temp) + '.tif', raster_datatype=gdal.GDT_Float32)
                                    info_temp = zonal_stats(polygon, f'/vsimem/{str(dc_doy_temp)}_{str(index_temp)}.tif', stats=['count', 'min', 'max', 'sum'], add_stats={'nanmean': no_nan_mean})
                                    gdal.Unlink('/vsimem/', str(dc_doy_temp) + '_' + str(index_temp) + '.tif')

                                    if ~np.isnan(info_temp[0]['nanmean']):
                                        para = True
                                        indi_temp = info_temp[0]['nanmean']
                                        ouput_doy_temp = dc_doy_temp
                                        break
                                    else:
                                        doy_list.remove(dc_doy_temp)

                            if para is True and indi_temp is not None:
                                GEDI_list_temp.GEDI_df[f'S2_nearest_{index_temp}_value'][i] = indi_temp
                                GEDI_list_temp.GEDI_df[f'S2_nearest_{index_temp}_date'][i] = ouput_doy_temp
                                break
                    i += 1

            elif self._GEDI_link_S2_retrieval_method == 'linear_interpolation':

                # Link GEDI and S2 inform using linear_interpolation
                i = 0
                GEDI_list_temp.GEDI_df.insert(loc=len(GEDI_list_temp.GEDI_df.columns), column=f'S2_{index_temp}_linear_interpolation',value=np.nan)

                while i <= GEDI_list_temp.df_size:

                    # Get the basic inform of the i GEDI point
                    lat = GEDI_list_temp.GEDI_df['Latitude'][i]
                    lon = GEDI_list_temp.GEDI_df['Longitude'][i]
                    date_temp = GEDI_list_temp.GEDI_df['Date'][i]
                    year_temp = int(date_temp) // 1000

                    # Draw a circle around a point
                    central_point = shapely.geometry.Point([lat, lon])  # location
                    n_points = 360 * 10
                    diameter = 30  # meters
                    angles = np.linspace(0, 360, n_points)
                    polygon_coordinate = geog.propagate(central_point, angles, diameter)
                    polygon = shapely.geometry.mapping(shapely.geometry.Polygon(polygon_coordinate))

                    doy_list = self.doy_list
                    data_postive, date_postive, data_negative, date_negative = None, None, None, None

                    for date_interval in range(0, 365):
                        if date_interval == 0:
                            info_temp = self._extract_value2shpfile(polygon, index_temp, date_temp)
                            if ~np.isnan(info_temp[0]['nanmean']):
                                GEDI_list_temp.GEDI_df[f'S2_{index_temp}_linear_interpolation'][i] = info_temp[0]['nanmean']
                                break
                        else:
                            if date_temp - date_interval in doy_list and data_negative is None:
                                date_temp_temp = date_temp - date_interval
                                info_temp = self._extract_value2shpfile(polygon, index_temp, date_temp_temp)
                                if ~np.isnan(info_temp[0]['nanmean']):
                                    data_negative = info_temp[0]['nanmean']
                                    date_negative = date_temp_temp

                            if date_temp + date_interval in doy_list and data_postive is None:
                                date_temp_temp = date_temp + date_interval
                                info_temp = self._extract_value2shpfile(polygon, index_temp, date_temp_temp)
                                if ~np.isnan(info_temp[0]['nanmean']):
                                    data_postive = info_temp[0]['nanmean']
                                    date_postive = date_temp_temp

                            if data_postive is not None and data_negative is not None:
                                GEDI_list_temp.GEDI_df[f'S2_{index_temp}_linear_interpolation'][i] = data_negative + (date_temp - date_negative) * (data_postive - data_negative) / (date_postive - date_negative)
                                break

                    i += 1


def eliminating_all_non_tif_file(file_path_f):
    filter_name = ['.TIF']
    tif_file_list = bf.file_filter(file_path_f, filter_name)
    for file in tif_file_list:
        if file[-4:] != '.TIF':
            try:
                os.remove(file)
            except:
                print('file cannot be removed')
                sys.exit(-1)


def remove_all_file_and_folder(filter_list):
    for file in filter_list:
        if os.path.isdir(str(file)):
            try:
                shutil.rmtree(file)
            except:
                print('folder cannot be removed')
        elif os.path.isfile(str(file)):
            try:
                os.remove(file)
            except:
                print('file cannot be removed')
        else:
            print(f'{str(file)} has been removed!')


# def s2_resample(temp_S2file):
#     parameters_resample = HashMap()
#     parameters_resample.put('targetResolution', 10)
#     temp_s2file_resample = snappy.GPF.createProduct('Resample', parameters_resample, temp_S2file)
#     temp_width = temp_s2file_resample.getSceneRasterWidth()
#     temp_height = temp_s2file_resample.getSceneRasterHeight()
#     ul_pos = temp_S2file.getSceneGeoCoding().getGeoPos(PixelPos(0, 0), None)
#     ur_pos = temp_S2file.getSceneGeoCoding().getGeoPos(PixelPos(0, temp_S2file.getSceneRasterWidth() - 1), None)
#     lr_pos = temp_S2file.getSceneGeoCoding().getGeoPos(
#         PixelPos(temp_S2file.getSceneRasterHeight() - 1, temp_S2file.getSceneRasterWidth() - 1), None)
#     ll_pos = temp_S2file.getSceneGeoCoding().getGeoPos(PixelPos(temp_S2file.getSceneRasterHeight() - 1, 0), None)
#     print(list(temp_s2file_resample.getBandNames()))
#     return temp_s2file_resample, temp_width, temp_height, ul_pos, ur_pos, lr_pos, ll_pos
#
#
# def s2_reprojection(product, crs):
#     parameters_reprojection = HashMap()
#     parameters_reprojection.put('crs', crs)
#     parameters_reprojection.put('resampling', 'Nearest')
#     product_reprojected = snappy.GPF.createProduct('Reproject', parameters_reprojection, product)
#     # ProductIO.writeProduct(product_reprojected, temp_filename, 'BEAM-DIMAP')
#     return product_reprojected
#
#
# def write_subset_band(temp_s2file_resample, band_name, subset_output_path, file_output_name):
#     parameters_subset_sd = HashMap()
#     parameters_subset_sd.put('sourceBands', band_name)
#     # parameters_subset_sd.put('copyMetadata', True)
#     temp_product_subset = snappy.GPF.createProduct('Subset', parameters_subset_sd, temp_s2file_resample)
#     subset_write_op = WriteOp(temp_product_subset, File(subset_output_path + file_output_name), 'GeoTIFF-BigTIFF')
#     subset_write_op.writeProduct(ProgressMonitor.NULL)
#
#     temp_product_subset.dispose()
#     del temp_product_subset
#     # temp_product_subset = None


def create_NDWI_NDVI_CURVE(NDWI_data_cube, NDVI_data_cube, doy_list, fig_path_f):
    if NDWI_data_cube.shape == NDVI_data_cube.shape and doy_list.shape[0] == NDWI_data_cube.shape[2]:
        start_year = doy_list[0] // 1000
        doy_num = []
        for doy in doy_list:
            doy_num.append((doy % 1000) + 365 * ((doy // 1000) - start_year))
        for y in range(NDVI_data_cube.shape[0] // 16, 9 * NDVI_data_cube.shape[0] // 16):
            for x in range(8 * NDVI_data_cube.shape[1] // 16, NDVI_data_cube.shape[1]):
                NDVI_temp_list = []
                NDWI_temp_list = []
                for z in range(NDVI_data_cube.shape[2]):
                    NDVI_temp_list.append(NDVI_data_cube[y, x, z])
                    NDWI_temp_list.append(NDWI_data_cube[y, x, z])

                plt.xlabel('DOY')
                plt.ylabel('ND*I')
                plt.xlim(xmax=max(doy_num), xmin=0)
                plt.ylim(ymax=1, ymin=-1)
                colors1 = '#006000'
                colors2 = '#87CEFA'
                area = np.pi * 3 ** 2
                plt.scatter(doy_num, NDVI_temp_list, s=area, c=colors1, alpha=0.4, label='NDVI')
                plt.scatter(doy_num, NDWI_temp_list, s=area, c=colors2, alpha=0.4, label='NDWI')
                plt.plot([0, 0.8], [max(doy_num), 0.8], linewidth='1', color='#000000')
                plt.legend()
                plt.savefig(fig_path_f + 'Scatter_plot_' + str(x) + '_' + str(y) + '.png', dpi=300)
                plt.close()
    else:
        print('The data and date shows inconsistency')


def cor_to_pixel(two_corner_coordinate, study_area_example_file_path):
    pixel_limitation_f = {}
    if len(two_corner_coordinate) == 2:
        UL_corner = two_corner_coordinate[0]
        LR_corner = two_corner_coordinate[1]
        if len(UL_corner) == len(LR_corner) == 2:
            upper_limit = UL_corner[1]
            lower_limit = LR_corner[1]
            right_limit = LR_corner[0]
            left_limit = UL_corner[0]
            dataset_temp_list = bf.file_filter(study_area_example_file_path, ['.TIF'])
            temp_dataset = gdal.Open(dataset_temp_list[0])
            # TEMP_warp = gdal.Warp(study_area_example_file_path + '\\temp.TIF', temp_dataset, dstSRS='EPSG:4326')
            # temp_band = temp_dataset.GetRasterBand(1)
            # temp_cols = temp_dataset.RasterXSize
            # temp_rows = temp_dataset.RasterYSize
            temp_transform = temp_dataset.GetGeoTransform()
            temp_xOrigin = temp_transform[0]
            temp_yOrigin = temp_transform[3]
            temp_pixelWidth = temp_transform[1]
            temp_pixelHeight = -temp_transform[5]
            pixel_limitation_f['x_max'] = max(int((right_limit - temp_xOrigin) / temp_pixelWidth),
                                              int((left_limit - temp_xOrigin) / temp_pixelWidth))
            pixel_limitation_f['y_max'] = max(int((temp_yOrigin - lower_limit) / temp_pixelHeight),
                                              int((temp_yOrigin - upper_limit) / temp_pixelHeight))
            pixel_limitation_f['x_min'] = min(int((right_limit - temp_xOrigin) / temp_pixelWidth),
                                              int((left_limit - temp_xOrigin) / temp_pixelWidth))
            pixel_limitation_f['y_min'] = min(int((temp_yOrigin - lower_limit) / temp_pixelHeight),
                                              int((temp_yOrigin - upper_limit) / temp_pixelHeight))
        else:
            print('Please make sure input all corner pixel with two coordinate in list format')
    else:
        print('Please mention the input coordinate should contain the coordinate of two corner pixel')
    try:
        # TEMP_warp.dispose()
        os.remove(study_area_example_file_path + '\\temp.TIF')
    except:
        print('please remove the temp file manually')
    return pixel_limitation_f


def check_vi_file_consistency(l2a_output_path_f, index_list):
    vi_file = []
    c_word = ['.TIF']
    r_word = ['.ovr']
    for vi in index_list:
        if not os.path.exists(l2a_output_path_f + vi):
            print(vi + 'folders are missing')
            sys.exit(-1)
        else:
            redundant_file_list = bf.file_filter(l2a_output_path_f + vi + '\\', r_word)
            remove_all_file_and_folder(redundant_file_list)
            tif_file_list = bf.file_filter(l2a_output_path_f + vi + '\\', c_word)
            vi_temp = []
            for tif_file in tif_file_list:
                vi_temp.append(tif_file[tif_file.find('\\20') + 2:tif_file.find('\\20') + 15])
            vi_file.append(vi_temp)
    for i in range(len(vi_file)):
        if not collections.Counter(vi_file[0]) == collections.Counter(vi_file[i]):
            print('VIs did not share the same file numbers')
            sys.exit(-1)


def f_two_term_fourier(x, a0, a1, b1, a2, b2, w):
    return a0 + a1 * np.cos(w * x) + b1 * np.sin(w * x) + a2 * np.cos(2 * w * x) + b2 * np.sin(2 * w * x)


def curve_fitting(l2a_output_path_f, index_list, study_area_f, pixel_limitation_f, fig_path_f, mndwi_threshold):
    # so, this is the Curve fitting Version 1, Generally it is used to implement two basic functions:
    # (1) Find the inundated pixel by introducing MNDWI with an appropriate threshold and remove it.
    # (2) Using the remaining data to fitting the vegetation growth curve
    # (3) Obtaining vegetation phenology information

    # Check whether the VI data cube exists or not
    VI_dic_sequenced = {}
    VI_dic_curve = {}
    doy_factor = False
    consistency_factor = True
    if 'NDWI' in index_list and os.path.exists(
            l2a_output_path_f + 'NDWI_' + study_area_f + '\\sequenced_data_cube\\' + 'sequenced_data_cube.npy') and os.path.exists(
        l2a_output_path_f + 'NDWI_' + study_area_f + '\\sequenced_data_cube\\' + 'doy_list.npy'):
        NDWI_sequenced_datacube_temp = np.load(
            l2a_output_path_f + 'NDWI_' + study_area_f + '\\sequenced_data_cube\\' + 'sequenced_data_cube.npy')
        NDWI_date_temp = np.load(
            l2a_output_path_f + 'NDWI_' + study_area_f + '\\sequenced_data_cube\\' + 'doy_list.npy')
        VI_list_temp = copy.copy(index_list)
        try:
            VI_list_temp.remove('QI')
        except:
            print('QI is not in the VI list')
        VI_list_temp.remove('NDWI')
        for vi in VI_list_temp:
            try:
                VI_dic_sequenced[vi] = np.load(
                    l2a_output_path_f + vi + '_' + study_area_f + '\\sequenced_data_cube\\' + 'sequenced_data_cube.npy')
                if not doy_factor:
                    VI_dic_sequenced['doy'] = np.load(
                        l2a_output_path_f + vi + '_' + study_area_f + '\\sequenced_data_cube\\' + 'doy_list.npy')
                    doy_factor = True
            except:
                print('Please make sure the forward programme has been processed')
                sys.exit(-1)

            if not (NDWI_date_temp == VI_dic_sequenced['doy']).all or not (
                    VI_dic_sequenced[vi].shape[2] == len(NDWI_date_temp)):
                consistency_factor = False
                print('Consistency problem occurred')
                sys.exit(-1)

        VI_dic_curve['VI_list'] = VI_list_temp
        for y in range(pixel_limitation_f['y_min'], pixel_limitation_f['y_max'] + 1):
            for x in range(pixel_limitation_f['x_min'], pixel_limitation_f['x_max'] + 1):
                VIs_temp = np.zeros((len(NDWI_date_temp), len(VI_list_temp) + 2))
                VIs_temp_curve_fitting = np.zeros((len(NDWI_date_temp), len(VI_list_temp) + 1))
                NDWI_threshold_cube = np.zeros(len(NDWI_date_temp))
                VIs_temp[:, 1] = copy.copy(NDWI_sequenced_datacube_temp[y, x, :])
                VIs_temp[:, 0] = ((VI_dic_sequenced['doy'] // 1000) - 2020) * 365 + VI_dic_sequenced['doy'] % 1000
                VIs_temp_curve_fitting[:, 0] = ((VI_dic_sequenced['doy'] // 1000) - 2020) * 365 + VI_dic_sequenced[
                    'doy'] % 1000

                NDWI_threshold_cube = copy.copy(VIs_temp[:, 1])
                NDWI_threshold_cube[NDWI_threshold_cube > mndwi_threshold] = np.nan
                NDWI_threshold_cube[NDWI_threshold_cube < mndwi_threshold] = 1
                NDWI_threshold_cube[np.isnan(NDWI_threshold_cube)] = np.nan

                i = 0
                for vi in VI_list_temp:
                    VIs_temp[:, i + 2] = copy.copy(VI_dic_sequenced[vi][y, x, :])
                    VIs_temp_curve_fitting[:, i + 1] = copy.copy(VI_dic_sequenced[vi][y, x, :]) * NDWI_threshold_cube
                    i += 1

                doy_limitation = np.where(VIs_temp_curve_fitting[:, 0] > 365)
                for i in range(len(doy_limitation)):
                    VIs_temp_curve_fitting = np.delete(VIs_temp_curve_fitting, doy_limitation[i], 0)

                nan_pos = np.where(np.isnan(VIs_temp_curve_fitting[:, 1]))
                for i in range(len(nan_pos)):
                    VIs_temp_curve_fitting = np.delete(VIs_temp_curve_fitting, nan_pos[i], 0)

                nan_pos2 = np.where(np.isnan(VIs_temp[:, 1]))
                for i in range(len(nan_pos2)):
                    VIs_temp = np.delete(VIs_temp, nan_pos2[i], 0)

                i_test = np.argwhere(np.isnan(VIs_temp_curve_fitting))
                if len(i_test) > 0:
                    print('consistency error')
                    sys.exit(-1)

                paras_temp = np.zeros((len(VI_list_temp), 6))

                curve_fitting_para = True
                for i in range(len(VI_list_temp)):
                    if VIs_temp_curve_fitting.shape[0] > 5:
                        paras, extras = curve_fit(f_two_term_fourier, VIs_temp_curve_fitting[:, 0],
                                                  VIs_temp_curve_fitting[:, i + 1], maxfev=5000,
                                                  p0=[0, 0, 0, 0, 0, 0.017], bounds=(
                                [-100, -100, -100, -100, -100, 0.014], [100, 100, 100, 100, 100, 0.020]))
                        paras_temp[i, :] = paras
                    else:
                        curve_fitting_para = False
                VI_dic_curve[str(y) + '_' + str(x) + 'curve_fitting_paras'] = paras_temp
                VI_dic_curve[str(y) + '_' + str(x) + 'ori'] = VIs_temp
                VI_dic_curve[str(y) + '_' + str(x) + 'curve_fitting'] = VIs_temp_curve_fitting

                x_temp = np.linspace(0, 365, 10000)
                # 'QI', 'NDVI', 'NDWI', 'EVI', 'EVI2', 'OSAVI', 'GNDVI', 'NDVI_RE', 'NDVI_2', 'NDVI_RE2'
                colors = {'colors_NDVI': '#00CD00', 'colors_NDVI_2': '#00EE00', 'colors_NDVI_RE': '#CDBE70',
                          'colors_NDVI_RE2': '#CDC673', 'colors_GNDVI': '#7D26CD', 'colors_NDWI': '#0000FF',
                          'colors_EVI': '#FFFF00', 'colors_EVI2': '#FFD700', 'colors_OSAVI': '#FF3030'}
                markers = {'markers_NDVI': 'o', 'markers_NDWI': 's', 'markers_EVI': '^', 'markers_EVI2': 'v',
                           'markers_OSAVI': 'p', 'markers_NDVI_2': 'D', 'markers_NDVI_RE': 'x', 'markers_NDVI_RE2': 'X',
                           'markers_GNDVI': 'd'}
                plt.rcParams["font.family"] = "Times New Roman"
                plt.figure(figsize=(10, 6))
                ax = plt.axes((0.1, 0.1, 0.9, 0.8))
                plt.xlabel('DOY')
                plt.ylabel('ND*I')
                plt.xlim(xmax=max(((VI_dic_sequenced['doy'] // 1000) - 2020) * 365 + VI_dic_sequenced['doy'] % 1000),
                         xmin=1)
                plt.ylim(ymax=1, ymin=-1)
                ax.tick_params(axis='x', which='major', labelsize=15)
                plt.xticks([15, 44, 75, 105, 136, 166, 197, 228, 258, 289, 320, 351, 380, 409, 440, 470, 501, 532],
                           ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', 'Jan',
                            'Feb', 'Mar', 'Apr', 'May', 'Jun'])
                plt.plot(np.linspace(365, 365, 1000), np.linspace(-1, 1, 1000), linestyle='--', color=[0.5, 0.5, 0.5])
                area = np.pi * 3 ** 2

                plt.scatter(VIs_temp[:, 0], VIs_temp[:, 1], s=area, c=colors['colors_NDWI'], alpha=1, label='NDWI')
                for i in range(len(VI_list_temp)):
                    plt.scatter(VI_dic_curve[str(y) + '_' + str(x) + 'curve_fitting'][:, 0],
                                VI_dic_curve[str(y) + '_' + str(x) + 'curve_fitting'][:, i + 1], s=area,
                                c=colors['colors_' + VI_list_temp[i]], alpha=1, norm=0.8, label=VI_list_temp[i],
                                marker=markers['markers_' + VI_list_temp[i]])
                    # plt.show()
                    if curve_fitting_para:
                        a0_temp, a1_temp, b1_temp, a2_temp, b2_temp, w_temp = VI_dic_curve[str(y) + '_' + str(
                            x) + 'curve_fitting_paras'][i, :]
                        plt.plot(x_temp,
                                 f_two_term_fourier(x_temp, a0_temp, a1_temp, b1_temp, a2_temp, b2_temp, w_temp),
                                 linewidth='1.5', color=colors['colors_' + VI_list_temp[i]])
                plt.legend()
                plt.savefig(fig_path_f + 'Scatter_plot_' + str(x) + '_' + str(y) + '.png', dpi=300)
                plt.close()
                print('Finish plotting Figure ' + str(x) + '_' + str(y))
        np.save(fig_path_f + 'fig_data.npy', VI_dic_curve)
    else:
        print('Please notice that NDWI is essential for inundated pixel removal')
        sys.exit(-1)


if __name__ == '__main__':
    # Create Output folder
    # filepath = 'G:\A_veg\S2_all\\Original_file\\'
    filepath = 'G:\A_veg\S2_test\\Orifile\\'
    s2_ds_temp = Sentinel2_ds(filepath)
    s2_ds_temp.construct_metadata()
    s2_ds_temp.sequenced_subset(['MNDWI', 'OSAVI'], ROI='E:\\A_Veg_phase2\\Sentinel_2_test\\shpfile\\Floodplain_2020.shp',
                                ROI_name='MYZR_FP_2020', cloud_removal_strategy='QI_all_cloud',
                                size_control_factor=True, combine_band_factor=True)
    # s2_ds_temp.mp_subset(['all_band', 'NDVI', 'MNDWI', 'OSAVI', 'RGB'], ROI='E:\\A_Veg_phase2\\Sample_Inundation\\Floodplain_Devised\\floodplain_2020.shp', ROI_name='MYZR_FP_2020', cloud_removal_strategy='QI_all_cloud', size_control_factor=True)
    s2_ds_temp.to_datacube(['NDVI', 'MNDWI', 'OSAVI'], inherit_from_logfile=True)
    file_path = 'E:\\A_PhD_Main_stuff\\2022_04_22_Mid_Yangtze\\Sample_Sentinel\\Original_Zipfile\\'
    output_path = 'E:\\A_PhD_Main_stuff\\2022_04_22_Mid_Yangtze\\Sample_Sentinel\\'
    l2a_output_path = output_path + 'Sentinel2_L2A_output\\'
    QI_output_path = output_path + 'Sentinel2_L2A_output\\QI\\'
    bf.create_folder(l2a_output_path)
    bf.create_folder(QI_output_path)

    # Generate VIs in GEOtiff format

    # # this allows GDAL to throw Python Exceptions
    # gdal.UseExceptions()
    # mask_path = 'E:\\A_Vegetation_Identification\\Wuhan_Sentinel_L2_Original\\Arcmap\\shp\\Huxianzhou.shp'
    # # Check VI file consistency
    # check_vi_file_consistency(l2a_output_path, VI_list)
    # study_area = mask_path[mask_path.find('\\shp\\') + 5: mask_path.find('.shp')]
    # specific_name_list = ['clipped', 'cloud_free', 'data_cube', 'sequenced_data_cube']
    # # Process files
    # VI_list = ['NDVI', 'NDWI']
    # vi_process(l2a_output_path, VI_list, study_area, specific_name_list, overwritten_para_clipped,
    #            overwritten_para_cloud, overwritten_para_datacube, overwritten_para_sequenced_datacube)

    # Inundated detection
    # Spectral unmixing
    # Curve fitting
    # mndwi_threshold = -0.15
    # fig_path = l2a_output_path + 'Fig\\'
    # pixel_limitation = cor_to_pixel([[778602.523, 3322698.324], [782466.937, 3325489.535]],
    #                                 l2a_output_path + 'NDVI_' + study_area + '\\cloud_free\\')
    # curve_fitting(l2a_output_path, VI_list, study_area, pixel_limitation, fig_path, mndwi_threshold)
    # Generate Figure
    # NDWI_DATA_CUBE = np.load(NDWI_data_cube_path + 'data_cube_inorder.npy')
    # NDVI_DATA_CUBE = np.load(NDVI_data_cube_path + 'data_cube_inorder.npy')
    # DOY_LIST = np.load(NDVI_data_cube_path + 'doy_list.npy')
    # fig_path = output_path + 'Sentinel2_L2A_output\\Fig\\'
    # create_folder(fig_path)
    # create_NDWI_NDVI_CURVE(NDWI_DATA_CUBE, NDVI_DATA_CUBE, DOY_LIST, fig_path)
