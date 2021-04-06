import numpy as np
import os
import zarr
import tifffile as tiff
import logging

# libraries for singlepage tiff sequence reading
import glob
import json
import natsort


class MicromanagerSequenceReader:

    def __init__(self,
                 folder,
                 extract_data):
        """
        reads single-page tiff files generated by micro-manager into zarr or numpy arrays
        Strategy:
            1. Gather summary metadata from any metadata.txt
            2. Build a map between image coordinates and image file names
            3. Upon call to extract data, assign each scene to self.positions (similar to OmeTiffReader)

        :param folder: str
            folder containing position subdirectories, which contain singlepage tiff sequences
        :param extract_data: bool
            True if zarr arrays should be extracted immediately
        """

        if not os.path.isdir(folder):
            raise NotImplementedError("supplied path for singlepage tiff sequence reader is not a folder")

        self.log = logging.getLogger(__name__)
        self.positions = {}
        self.mm_meta = None
        self.stage_positions = 0
        self.height = 0
        self.width = 0
        self.frames = 0
        self.slices = 0
        self.channels = 0
        self.channel_names = []

        self.coord_to_filename = {}

        # identify type of subdirectory
        sub_dirs = self._get_sub_dirs(folder)
        if sub_dirs:
            pos_path = os.path.join(folder, sub_dirs[0])
        else:
            raise AttributeError("supplied folder does not contain position or default subdirectories")

        # pull one metadata sample and extract experiment dimensions
        metadata_path = os.path.join(pos_path, 'metadata.txt')
        with open(metadata_path, 'r') as f:
            self.mm_meta = json.load(f)

        self.mm_version = self.mm_meta['Summary']['MicroManagerVersion']
        if self.mm_version == '1.4.22':
            self._mm1_meta_parser()
        elif 'beta' in self.mm_version:
            self._mm2beta_meta_parser()
        elif 'gamma' in self.mm_version:
            self._mm2gamma_meta_parser()
        else:
            raise NotImplementedError(
                f'Current MicroManager reader only supports version 1.4.22 and 2.0 but {self.mm_version} was detected')

        # create coordinate to filename maps
        self.coord_to_filename = self.read_tiff_series(folder)

        # todo: consider iterating over all positions.  Doable once we add a metadata search for stage positions
        if extract_data:
            self._create_stores(0)

    def get_zarr(self, position_):
        if position_ not in self.positions.keys():
            self._create_stores(position_)
        return self.positions[position_]

    def get_array(self, position_):
        if position_ not in self.positions.keys():
            self._create_stores(position_)
        return np.array(self.positions[position_])

    def get_num_positions(self):
        self.log.warning("num positions for singlepage tiff reader is ambiguous.  only loaded positions are reported")
        if self.positions:
            return len(self.positions)
        else:
            self.log.error("singlepage tiffs not loaded")

    def _create_stores(self, p):
        """
        extract all singlepage tiffs at each coordinate and place them in a zarr array
        coordinates are of shape = (pos, time, channel, z)
        :return:
        """
        self.log.info("")
        z = zarr.zeros(shape=(self.frames,
                              self.channels,
                              self.slices,
                              self.height,
                              self.width),
                       chunks=(1,
                               1,
                               1,
                               self.height,
                               self.width))
        for c, fn in self.coord_to_filename.items():
            if c[0] == p:
                self.log.info(f"reading coord = {c} from filename = {fn}")
                z[c[1], c[2], c[3]] = zarr.open(tiff.imread(fn, aszarr=True))
        self.positions[p] = z

    def read_tiff_series(self, folder: str):
        """
        given a folder containing position subfolders, each of which contains
            single-page-tiff series acquired in mm2.0 gamma, parse the metadata
            to map image coordinates to filepaths/names
        :param folder: str
        :return: dict
            keys are coordinates and values are filenames.  Coordinates follow (p, t, c, z) indexing.
        """
        positions = [p for p in os.listdir(folder) if os.path.isdir(os.path.join(folder, p))]
        if not positions:
            raise FileNotFoundError("no position subfolder found in supplied folder")

        metadatas = [os.path.join(folder, position, 'metadata.txt') for position in positions]
        if not metadatas:
            raise FileNotFoundError("no metadata.txt file found in position directories")

        coord_filename_map = {}
        for idx, metadata in enumerate(metadatas):
            with open(metadata, 'r+') as m:
                j = json.load(m)
                coord_filename_map.update(self._extract_coord_to_filename(j,
                                                                          folder,
                                                                          positions[idx]))

        return coord_filename_map

    def _extract_coord_to_filename(self,
                                   json_,
                                   parent_folder,
                                   position=None):
        """
        given a micro-manager generated metadata json, extract image coordinates and their corresponding image filepaths
        build a mapping between the two.
        :param json_: dict
            dict generated from json.load
        :param parent_folder: str
            full path to file
        :param position: str
            mm1.4.22 metadata does not associate positions with images in the metadata.  This has to be provided.
        :return:
        """
        coords = set()
        meta = dict()

        # separate coords from meta
        for element in json_.keys():
            # present for mm2-gamma metadata
            if "Coords" in element:
                coords.add(element)
            if "Metadata" in element:
                meta[element.split('-')[2]] = element

            # present in mm1.4.22 metadata
            if "FrameKey" in element:
                coords.add(element)

        if not coords:
            raise ValueError("no image coordinates present in metadata")

        # build a dict of coord to filename maps
        coord_to_filename = dict()
        for c in coords:
            # indices common to both mm2 and mm1
            ch_idx = json_[c]['ChannelIndex']
            pos_idx = json_[c]['PositionIndex']
            time_idx = json_[c]['FrameIndex']
            z_idx = json_[c]['SliceIndex']

            # extract filepath for this coordinate
            try:
                # for mm2-gamma. filename contains position folder
                if c.split('-')[2] in meta:
                    filepath = json_[meta[c.split('-')[2]]]['FileName']
                # for mm1, file name does not contain position folder
                else:
                    filepath = json_[c]['FileName']
                    filepath = os.path.join(position, filepath)  # position name is not present in metadata
            except KeyError as ke:
                self.log.error(f"metadata for supplied image coordinate {c} not found")
                raise ke

            coordinate = (pos_idx, time_idx, ch_idx, z_idx)
            coord_to_filename[coordinate] = os.path.join(parent_folder, filepath)

        return coord_to_filename

    def _get_sub_dirs(self, f):
        """
        subdir walk
        from https://github.com/mehta-lab/reconstruct-order

        :param f: str
        :return: list
        """
        sub_dir_path = glob.glob(os.path.join(f, '*/'))
        sub_dir_name = [os.path.split(subdir[:-1])[1] for subdir in sub_dir_path]
        #    assert subDirName, 'No sub directories found'
        return natsort.natsorted(sub_dir_name)

    def _mm1_meta_parser(self):
        """
        set image metadata.
        from https://github.com/mehta-lab/reconstruct-order

        :return:
        """
        self.width = self.mm_meta['Summary']['Width']
        self.height = self.mm_meta['Summary']['Height']
        self.frames = self.mm_meta['Summary']['Frames']
        self.slices = self.mm_meta['Summary']['Slices']
        self.channels = self.mm_meta['Summary']['Channels']

    def _mm2beta_meta_parser(self):
        """
        set image metadata
        from https://github.com/mehta-lab/reconstruct-order
        :return:
        """
        self.width = int(self.mm_meta['Summary']['UserData']['Width']['PropVal'])
        self.height = int(self.mm_meta['Summary']['UserData']['Height']['PropVal'])
        self.time_stamp = self.mm_meta['Summary']['StartTime']

    def _mm2gamma_meta_parser(self):
        """
        set image metadata
        from https://github.com/mehta-lab/reconstruct-order
        :return:
        """
        keys_list = list(self.mm_meta.keys())
        if 'FrameKey-0-0-0' in keys_list[1]:
            roi_string = self.mm_meta[keys_list[1]]['ROI']
            self.width = int(roi_string.split('-')[2])
            self.height = int(roi_string.split('-')[3])
        elif 'Metadata-' in keys_list[2]:
            self.width = self.mm_meta[keys_list[2]]['Width']
            self.height = self.mm_meta[keys_list[2]]['Height']
        else:
            raise ValueError('Metadata file incompatible with metadata reader')
        self.frames = self.mm_meta['Summary']['Frames']
        self.slices = self.mm_meta['Summary']['Slices']
        self.channels = self.mm_meta['Summary']['Channels']