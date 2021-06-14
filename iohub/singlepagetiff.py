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

        Parameters
        ----------
        folder:         (str) folder containing position subdirectories, which contain singlepage tiff sequences
        extract_data    (bool) True if zarr arrays should be extracted immediately
        """

        if not os.path.isdir(folder):
            raise NotImplementedError("supplied path for singlepage tiff sequence reader is not a folder")

        self.log = logging.getLogger(__name__)
        self.positions = {}
        self.num_positions = None
        self.mm_meta = None
        self.stage_positions = 0
        self.z_step_size = None
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

        self._set_mm_meta(pos_path)

        # create coordinate to filename maps
        self.coord_to_filename = self.read_tiff_series(folder)

        # update coordinates if the acquisition finished early
        self._dims_from_coordinates()

        # todo: consider iterating over all positions.  Doable once we add a metadata search for stage positions
        if extract_data:
            self._create_stores(0)

    def _set_mm_meta(self, one_pos):
        """
        assign image metadata from summary metadata

        Parameters
        ----------
        one_pos:        (str) path to one position subfolder

        Returns
        -------

        """
        # pull one metadata sample and extract experiment dimensions
        metadata_path = os.path.join(one_pos, 'metadata.txt')
        with open(metadata_path, 'r') as f:
            self.mm_meta = json.load(f)

        mm_version = self.mm_meta['Summary']['MicroManagerVersion']
        if mm_version == '1.4.22':
            self._mm1_meta_parser()
        elif 'beta' in mm_version:
            self._mm2beta_meta_parser()
        elif 'gamma' in mm_version:
            self._mm2gamma_meta_parser()
        else:
            raise NotImplementedError(
                f'Current MicroManager reader only supports version 1.4.22 and 2.0 but {mm_version} was detected')

    def get_zarr(self, position):
        """
        return a zarr array for a given position

        Parameters
        ----------
        position:       (int) position (aka ome-tiff scene)

        Returns
        -------
        position:       (zarr.array)

        """
        if position not in self.positions.keys():
            self._create_stores(position)
        return self.positions[position]

    def get_array(self, position):
        """
        return a numpy array for a given position

        Parameters
        ----------
        position:   (int) position (aka ome-tiff scene)

        Returns
        -------
        position:   (np.ndarray)

        """
        if position not in self.positions.keys():
            self.log.info(f"position {position} not yet extracted, extracting ...")
            self._create_stores(position)
        return np.array(self.positions[position])

    def get_num_positions(self):
        """
        get total number of scenes referenced in ome-tiff metadata

        Returns
        -------
        number of positions     (int)

        """
        # self.log.warning("num positions for singlepage tiff reader is ambiguous.  only loaded positions are reported")
        if self.positions:
            return self.num_positions
            # return len(self.positions)
        else:
            self.log.error("singlepage tiffs not loaded")

    def _create_stores(self, p):
        """
        extract all singlepage tiffs at each coordinate and place them in a zarr array
        coordinates are of shape = (pos, time, channel, z)
        arrays are of shape = (time, channel, z, height, width)

        Parameters
        ----------
        p:      (int) position (aka ome-tiff scene) to extract

        Returns
        -------

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

        # check that the array was assigned
        if z == zarr.zeros(shape=(self.frames, self.channels, self.slices, self.height, self.width),
                           chunks=(1, 1, 1, self.height, self.width)):
            raise IOError(f"array at position {p} can not be found")

        self.positions[p] = z

    def read_tiff_series(self, folder: str):
        """
        given a folder containing position subfolders, each of which contains
            single-page-tiff series acquired using micro-manager, parse the metadata
            to map image coordinates to filepaths/names

        Parameters
        ----------
        folder:             (str) project folder containing all position subfolders

        Returns
        -------
        coord_filename_map  (dict) keys are coordinates and values are filenames.
                            Coordinates follow (p, t, c, z) indexing.
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
                coord_filename_map.update(self._extract_coord_to_filename(j, folder, positions[idx]))
        self.num_positions = len(positions)

        return coord_filename_map

    def _extract_coord_to_filename(self,
                                   json_,
                                   parent_folder,
                                   position):
        """
        given a micro-manager generated metadata json, extract image coordinates and their corresponding image filepaths
        build a mapping between the two.

        Parameters
        ----------
        json_:              (dict) dict generated from json.load of mm metadata
        parent_folder:      (str) full path to file
        position:           (str) mm1.4.22 metadata does not associate positions with images in the metadata.
                            This has to be provided.
                            This parameter is not used for mm2.0 metadata, which already provides positions

        Returns
        -------
        coord_to_filename   (dict) dictionary mapping of (key, value) = (coordinate, filepath)
                            where coordinate = (position, time, channel, z-slice)
        """

        coords = set()
        meta = dict()

        # separate coords from meta
        for element in json_.keys():
            # present for mm2-gamma metadata
            if "Coords" in element:
                coords.add(element)
            if "Metadata" in element:
                meta['-'.join(element.split('-')[1:])] = element

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
                # for mm2-gamma. 'FileName' key contains position folder
                if '-'.join(c.split('-')[1:]) in meta:
                    filepath = json_[meta['-'.join(c.split('-')[1:])]]['FileName']
                # for mm1, 'FileName' key does not contain position folder
                else:
                    filepath = json_[c]['FileName']
                    filepath = os.path.join(position, filepath)  # position name is explicitly supplied
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

        Parameters
        ----------
        f:              (str)

        Returns
        -------
        sub_dir_name    (list) natsorted list of subdirectories
        """

        sub_dir_path = glob.glob(os.path.join(f, '*/'))
        sub_dir_name = [os.path.split(subdir[:-1])[1] for subdir in sub_dir_path]
        #    assert subDirName, 'No sub directories found'
        return natsort.natsorted(sub_dir_name)

    def _dims_from_coordinates(self):
        """
        read coordinates from self.coord_to_filename
        parse the coordinates for the total number unique elements in each tuple position
            this total number reflects the true dimensionality
        coord = (pos, time, chan, z)
        height and width are still read from mm metadata
        Returns
        -------

        """
        p, t, c, z = set(), set(), set(), set()
        for coord in self.coord_to_filename.keys():
            p.add(coord[0])
            t.add(coord[1])
            c.add(coord[2])
            z.add(coord[3])
        self.frames = len(t)
        self.slices = len(z)
        self.channels = len(c)

    def _mm1_meta_parser(self):
        """
        set image metadata.
        from https://github.com/mehta-lab/reconstruct-order

        Returns
        -------

        """
        self.z_step_size = self.mm_meta['Summary']['z-step_um']
        self.width = self.mm_meta['Summary']['Width']
        self.height = self.mm_meta['Summary']['Height']
        self.frames = self.mm_meta['Summary']['Frames']
        self.slices = self.mm_meta['Summary']['Slices']
        self.channels = self.mm_meta['Summary']['Channels']

    def _mm2beta_meta_parser(self):
        """
        set image metadata
        from https://github.com/mehta-lab/reconstruct-order

        Returns
        -------

        """
        self.z_step_size = self.mm_meta['Summary']['z-step_um']
        self.width = int(self.mm_meta['Summary']['UserData']['Width']['PropVal'])
        self.height = int(self.mm_meta['Summary']['UserData']['Height']['PropVal'])
        self.time_stamp = self.mm_meta['Summary']['StartTime']

    def _mm2gamma_meta_parser(self):
        """
        set image metadata
        from https://github.com/mehta-lab/reconstruct-order

        Returns
        -------

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

        self.z_step_size = self.mm_meta['Summary']['z-step_um']
        self.frames = self.mm_meta['Summary']['Frames']
        self.slices = self.mm_meta['Summary']['Slices']
        self.channels = self.mm_meta['Summary']['Channels']

    @property
    def shape(self):
        """
        return the underlying data shape as a tuple

        Returns
        -------
        (tuple) five elements of (frames, slices, channels, height, width)

        """
        return self.frames, self.channels, self.slices, self.height, self.width
