import os
import sys
import binascii
import io
import numpy as np
from .db import surfs

def unmask(mask, data):
    """unmask(mask, data)

    "Unmasks" the data, assuming it's been masked.

    Parameters
    ----------
    mask : array_like
        The data mask
    data : array_like
        Actual MRI data to unmask
    """
    if data.ndim > 1:
        output = np.zeros((len(data),)+mask.shape, dtype=data.dtype)
        output[:, mask > 0] = data
    else:
        output = np.zeros(mask.shape, dtype=data.dtype)
        output[mask > 0] = data
    return output

def detrend_volume_median(data, kernel=15):
    from scipy.signal import medfilt
    lowfreq = medfilt(data, [1, kernel, kernel])
    return data - lowfreq

def detrend_volume_gradient(data, diff=3):
    return (np.array(np.gradient(data, 1, diff, diff))**2).sum(0)

def detrend_volume_poly(data, polyorder = 10, mask=None):
    from scipy.special import legendre
    polys = [legendre(i) for i in range(polyorder)]
    s = data.shape
    b = data.ravel()[:,np.newaxis]
    lins = np.mgrid[-1:1:s[0]*1j, -1:1:s[1]*1j, -1:1:s[2]*1j].reshape(3,-1)

    if mask is not None:
        lins = lins[:,mask.ravel() > 0]
        b = b[mask.ravel() > 0]
    
    A = np.vstack([[p(i) for i in lins] for p in polys]).T
    x, res, rank, sing = np.linalg.lstsq(A, b)

    detrended = b.ravel() - np.dot(A, x).ravel()
    if mask is not None:
        filled = np.zeros_like(mask)
        filled[mask > 0] = detrended
        return filled
    else:
        return detrended.reshape(*s)


def mosaic(data, xy=(6, 5), trim=10, skip=1, show=True, **kwargs):
    """mosaic(data, xy=(6, 5), trim=10, skip=1)

    Turns volume data into a mosaic, useful for quickly viewing volumetric data
    IN RADIOLOGICAL COORDINATES (LEFT SIDE OF FIGURE IS RIGHT SIDE OF SUBJECT)

    Parameters
    ----------
    data : array_like
        3D volumetric data to mosaic
    xy : tuple, optional
        tuple(x, y) for the grid of images. Default (6, 5)
    trim : int, optional
        How many pixels to trim from the edges of each image. Default 10
    skip : int, optional
        How many slices to skip in the beginning. Default 1
    """
    assert len(data.shape) == 3, "Are you sure this is volumetric?"
    dat = data.copy()
    if trim>0:
        dat = dat[:, trim:-trim, trim:-trim]
    d = dat.shape[1:]
    output = np.zeros(d*np.array(xy))
    
    c = skip
    for i in range(xy[0]):
        for j in range(xy[1]):
            if c < len(dat):
                output[d[0]*i:d[0]*(i+1), d[1]*j:d[1]*(j+1)] = dat[c]
            c+= 1
    
    if show:
        from matplotlib import pyplot as plt
        plt.imshow(output, **kwargs)
        plt.xticks([])
        plt.yticks([])

    return output

def get_mapper(subject, xfmname, type='nearest', **kwargs):
    from . import mapper
    mapfunc = dict(
        nearest=mapper.Nearest,
        trilinear=mapper.Trilinear,
        gaussian=mapper.Gaussian,
        polyhedral=mapper.Polyhedral,
        lanczos=mapper.Lanczos)
    return mapfunc[type](subject, xfmname, **kwargs)

def get_roipack(subject, remove_medial=False):
    from . import svgroi
    flat, polys, norms = surfs.getVTK(subject, "flat", merge=True, nudge=True)
    if remove_medial:
        valid = np.unique(polys)
        flat = flat[valid]
    svgfile = surfs.getFiles(subject)['rois']
    if not os.path.exists(svgfile):
        with open(svgfile, "w") as fp:
            fp.write(svgroi.make_svg(flat.copy(), polys))
    rois = svgroi.ROIpack(flat[:,:2], svgfile)
    if remove_medial:
        return rois, valid
    return rois

def get_ctmpack(subject, xfmname, types=("inflated",), projection='nearest', method="raw", level=0, recache=False, recache_mapper=False):
    ctmform = surfs.getFiles(subject)['ctmcache']
    ctmfile = ctmform.format(xfmname=xfmname, types=','.join(types), method=method, level=level)
    mapper = get_mapper(subject, xfmname, projection, recache=recache_mapper)
    if os.path.exists(ctmfile) and not recache:
        mapfile = os.path.splitext(ctmfile)[0]+'.npz'
        if os.path.exists(mapfile):
            ptmap = np.load(mapfile)
            mapper.idxmap = ptmap['left'], ptmap['right']
        return ctmfile, mapper

    print("Generating new ctm file...")
    from . import brainctm
    ptmap = brainctm.make_pack(ctmfile, subject, xfmname, types, method, level)
    if ptmap is not None:
        mapper.idxmap = ptmap
    return ctmfile, mapper

def get_cortical_mask(subject, xfmname, type='nearest'):
    return get_mapper(subject, xfmname, type=type).mask

def get_vox_dist(subject, xfmname):
    """Get the distance (in mm) from each functional voxel to the closest
    point on the surface.

    Parameters
    ----------
    subject : str
        Name of the subject
    xfmname : str
        Name of the transform
    shape : tuple
        Output shape for the mask

    Returns
    -------
    dist : ndarray
        Distance (in mm) to the closest point on the surface

    argdist : ndarray
        Point index for the closest point
    """
    import nibabel
    from scipy.spatial import cKDTree
    shape = nibabel.load(surfs.getXfm(subject, xfmname)[1]).shape[::-1]
    if len(shape) > 3:
        shape = shape[1:]

    fiducial, polys, norms = surfs.getVTK(subject, "fiducial", merge=True)
    xfm, epi = surfs.getXfm(subject, xfmname)
    idx = np.mgrid[:shape[0], :shape[1], :shape[2]].reshape(3, -1).T
    widx = np.append(idx[:,::-1], np.ones((len(idx),1)), axis=-1).T
    mm = np.dot(np.linalg.inv(xfm), widx)[:3].T

    tree = cKDTree(fiducial)
    dist, argdist = tree.query(mm)
    dist.shape = shape
    argdist.shape = shape
    return dist, argdist


def get_hemi_masks(subject, xfmname, type='nearest'):
    '''Returns a binary mask of the left and right hemisphere
    surface voxels for the given subject.
    '''
    return get_mapper(subject, xfmname, type=type).hemimasks

def add_roi(data, subject, xfmname, name="new_roi", recache=False, open_inkscape=True, add_path=True, projection='nearest', **kwargs):
    import subprocess as sp
    from matplotlib.pylab import imsave
    from .utils import get_roipack
    from . import quickflat
    rois = get_roipack(subject)
    im = quickflat.make(data, subject, xfmname, height=1024, recache=recache, projection=projection, with_rois=False)
    fp = io.StringIO()
    imsave(fp, im, **kwargs)
    fp.seek(0)
    rois.add_roi(name, binascii.b2a_base64(fp.read()), add_path)
    if open_inkscape:
        return sp.call(["inkscape", '-f', rois.svgfile])

def get_roi_verts(subject, roi=None):
    '''Return vertices for the given ROIs'''
    rois = get_roipack(subject)

    if roi is None:
        roi = rois.names

    roidict = dict()
    if isinstance(roi, str):
        roi = [roi]

    for name in roi:
        roidict[name] = rois.get_roi(name)

    return roidict

def get_roi_mask(subject, xfmname, roi=None, projection='nearest'):
    '''Return a bitmask for the given ROIs'''

    mapper = get_mapper(subject, xfmname, type=projection)
    rois = get_roi_verts(subject, roi=roi)
    output = dict()
    for name, verts in list(rois.items()):
        left, right = mapper.backwards(verts)
        output[name] = left + right
        
    return output

def get_roi_masks(subject,xfmname,roiList=None,Dst=2,overlapOpt='cut'):
    '''
    Return a numbered mask + dictionary of roi numbers
    roiList is a list of ROIs (which better be defined in the .svg file)
    poop.
    '''
    # Get ROIs from inkscape SVGs
    rois, vertIdx = get_roipack(subject, remove_medial=True)

    # Retrieve shape from the reference
    import nibabel
    shape = nibabel.load(surfs.getXfm(subject, xfmname)[1]).shape[::-1]
    if len(shape) > 3:
        shape = shape[1:]
    
    # Get 3D coords
    coords = np.vstack(surfs.getCoords(subject, xfmname))
    nVerts = np.max(coords.shape)
    coords = coords[vertIdx]
    nValidVerts = np.max(coords.shape)
    # Get voxDst,voxIdx (voxIdx has NOT had invalid 2-D vertices removed by "vertIdx" index)
    voxDst,voxIdx = get_vox_dist(subject,xfmname)
    voxIdxF = voxIdx.flatten()
    # Get L,R hem separately
    L,R = surfs.getVTK(subject, "flat", merge=False, nudge=True)
    nL = len(np.unique(L[1]))
    #nVerts = len(idxL)+len(idxR)
    # mask for left hemisphere
    Lmask = (voxIdx < nL).flatten()
    Rmask = np.logical_not(Lmask)
    CxMask = (voxDst < Dst).flatten()
    
    #return rois, flat, coords, voxDst, voxIdx ## rois is a list of class svgROI; flat = flat cortex coords; coords = 3D coords
    if roiList is None:
        roiList = rois.names

    if isinstance(roiList, str):
        roiList = [roiList]
    # First: get all roi voxels into 4D volume
    tmpMask = np.zeros((np.prod(shape),len(roiList),2),np.bool)
    for ir,roi in enumerate(roiList):
        if roi.lower()=='cortex':
            roiIdxB3 = np.ones(Lmask.shape)>0
        else:
            # Irritating index switching:
            roiIdxB1 = np.zeros((nValidVerts,),np.bool) # binary index 1
            roiIdxS1 = rois.get_roi(roi) # substitution index 1 (in valid vertex space)
            roiIdxB1[roiIdxS1] = True
            roiIdxB2 = np.zeros((nVerts,),np.bool) # binary index 2
            roiIdxB2[vertIdx] = roiIdxB1
            roiIdxS2 = np.nonzero(roiIdxB2)[0] # substitution index 2 (in ALL fiducial vertex space)
            roiIdxB3 = np.in1d(voxIdxF,roiIdxS2) # binary index to 3D volume (flattened, though)
        tmpMask[:,ir,0] = np.all(np.array([roiIdxB3,Lmask,CxMask]),axis=0)
        tmpMask[:,ir,1] = np.all(np.array([roiIdxB3,Rmask,CxMask]),axis=0)
    roiListL = [r.lower() for r in roiList]
    # Kill all overlap btw. "Cortex" and other ROIs
    if 'cortex' in roiListL:
        cIdx = roiListL.index('cortex')
        # Left:
        OtherROIs = tmpMask[:,np.arange(len(roiList))!=cIdx,0]
        tmpMask[:,cIdx,0] = np.logical_and(np.logical_not(np.any(OtherROIs,axis=1)),tmpMask[:,cIdx,0])
        # Right:
        OtherROIs = tmpMask[:,np.arange(len(roiList))!=cIdx,1]
        tmpMask[:,cIdx,1] = np.logical_and(np.logical_not(np.any(OtherROIs,axis=1)),tmpMask[:,cIdx,1])

    # Second: 
    mask = np.zeros(np.prod(shape),dtype=np.int64)
    roiIdx = {}
    if overlapOpt=='cut':
        toCut = np.sum(tmpMask,axis=1)>1
        # Note that indexing by voxIdx guarantees that there will be no overlap in ROIs
        # (unless there are overlapping assignments to ROIs on the surface), due to 
        # each voxel being assigned only ONE closest vertex
        print(('%d voxels cut'%np.sum(toCut)))
        tmpMask[toCut] = False 
        for ir,roi in enumerate(roiList):
            mask[tmpMask[:,ir,0]] = -ir-1
            mask[tmpMask[:,ir,1]] = ir+1
            roiIdx[roi] = ir+1
        mask.shape = shape
    elif overlapOpt=='split':
        pass
    return mask,roiIdx

def get_curvature(subject, smooth=8, neighborhood=3):
    from tvtk.api import tvtk
    curvs = []
    for hemi in surfs.getVTK(subject, "fiducial"):
        pd = tvtk.PolyData(points=hemi[0], polys=hemi[1])
        curv = tvtk.Curvatures(input=pd, curvature_type="mean")
        curv.update()
        curv = curv.output.point_data.scalars.to_array()
        if smooth == 0:
            curvs.append(curv)
        else:
            faces = dict()
            for poly in hemi[1]:
                for pt in poly:
                    if pt not in faces:
                        faces[pt] = set()
                    faces[pt] |= set(poly)

            def getpts(pt, n):
                if pt in faces:
                    for p in faces[pt]:
                        if n == 0:
                            yield p
                        else:
                            for q in getpts(p, n-1):
                                yield q

            curvature = np.zeros(len(hemi[0]))
            for i, pt in enumerate(hemi[0]):
                neighbors = list(set(getpts(i, neighborhood)))
                if len(neighbors) > 0:
                    g = np.exp(-(((hemi[0][neighbors] - pt)**2) / (2*smooth**2)).sum(1))
                    curvature[i] = (g * curv[neighbors]).mean()
                
            curvs.append(curvature)

    return curvs

def decimate_mesh(subject, proportion = 0.5):
    from scipy.spatial import Delaunay
    from .polyutils import trace_both
    flat = surfs.getVTK(subject, "flat")
    fiducial = surfs.getVTK(subject, "fiducial")
    edges = list(map(np.array, trace_both(*surfs.getVTK(subject, "flat", merge=True, nudge=True)[:2])))
    edges[1] -= len(flat[0][0])

    masks, newpolys = [], []
    for (fpts, fpolys, _), (pts, polys, _), edge in zip(flat, fiducial, edges):
        valid = np.unique(polys)

        edge_set = set(edge)

        mask = np.zeros((len(pts),), dtype=bool)
        mask[valid] = True
        mask[np.random.permutation(len(pts))[:len(pts)*(1-proportion)]] = False
        mask[edge] = True
        midx = np.nonzero(mask)[0]

        tri = Delaunay(fpts[mask, :2])
        #cull all the triangles from concave surfaces
        pmask = np.array([midx[p] in edge_set for p in tri.vertices.ravel()]).reshape(-1, 3).all(1)

        cutfaces = np.array([p in edge_set for p in polys.ravel()]).reshape(-1, 3).all(1)

        newpolys.append(tri.vertices[~pmask])
        fullpolys.append()
        masks.append(mask)

    return masks, newpolys

def get_flatmap_distortion(sub, type="areal"):
    """Computes distortion of flatmap relative to fiducial surface. Several different
    types of distortion are available:
    
    'areal': computes the areal distortion for each triangle in the flatmap, defined as the
    log ratio of the area in the fiducial mesh to the area in the flat mesh. Returns
    a per-vertex value that is the average of the neighboring triangles.
    See: http://brainvis.wustl.edu/wiki/index.php/Caret:Operations/Morphing
    
    'metric': computes the linear distortion for each vertex in the flatmap, defined as
    the mean squared difference between distances in the fiducial map and distances in
    the flatmap, for each pair of neighboring vertices. See Fishl, Sereno, and Dale, 1999.
    """
    distortions = []
    for hem in ["lh", "rh"]:
        fidvert, fidtri, etc = surfs.getVTK(sub, "fiducial", hem)
        flatvert, flattri, etc = surfs.getVTK(sub, "flat", hem)

        if type=="areal":
            triareas = lambda tr,v: np.array([np.linalg.norm(np.cross(a-b, a-c))/2
                                              for a,b,c in v[tr,:]])

            fidareas = triareas(flattri, fidvert)
            flatareas = triareas(flattri, flatvert)

            vertratios = np.zeros((len(fidvert),))
            vertratios[flattri[:,0]] += flatareas/fidareas
            vertratios[flattri[:,1]] += flatareas/fidareas
            vertratios[flattri[:,2]] += flatareas/fidareas
            vertratios /= np.bincount(flattri.ravel())
            vertratios = np.nan_to_num(vertratios)
            vertratios[vertratios==0] = 1
            distortions.append(np.log(vertratios))
            
        elif type=="metric":
            import networkx as nx
            def iter_surfedges(tris):
                for a,b,c in tris:
                    yield a,b
                    yield b,c
                    yield a,c

            def make_surface_graph(tris):
                graph = nx.Graph()
                graph.add_edges_from(iter_surfedges(tris))
                return graph

            G = make_surface_graph(flattri)
            selverts = np.unique(flattri.ravel())
            fid_dists = [np.sqrt(((fidvert[G.neighbors(ii)] - fidvert[ii])**2).sum(1))
                         for ii in selverts]
            flat_dists = [np.sqrt(((flatvert[G.neighbors(ii)] - flatvert[ii])**2).sum(1))
                          for ii in selverts]
            msdists = np.array([(fl-fi).mean() for fi,fl in zip(fid_dists, flat_dists)])
            alldists = np.zeros((len(fidvert),))
            alldists[selverts] = msdists
            distortions.append(alldists)

    return distortions

def get_tissots_indicatrix(sub, radius=10, spacing=50, maxfails=100):
    import networkx as nx
    def iter_surfedges(tris):
        for a,b,c in tris:
            yield a,b
            yield b,c
            yield a,c

    def make_surface_graph(tris):
        graph = nx.Graph()
        graph.add_edges_from(iter_surfedges(tris))
        return graph

    def dilate_vertset(vset, graph, iters=1):
        outset = set(vset)
        for ii in range(iters):
            newset = set(outset)
            for v in outset:
                newset.update(graph[v].keys())
            outset = set(newset)
        return outset

    def get_path_len_mm(path, verts):
        if len(path)==1:
            return 0
        hops = zip(path[:-1], path[1:])
        hopvecs = np.vstack([verts[a] - verts[b] for a,b in hops])
        return np.sqrt((hopvecs**2).sum(1)).sum()

    def memo_get_path_len_mm(path, verts, memo=dict()):
        if len(path)==1:
            return 0
        if tuple(path) in memo:
            return memo[tuple(path)]
        lasthoplen = np.sqrt(((verts[path[-2]] - verts[path[-1]])**2).sum())
        pathlen = memo_get_path_len_mm(path[:-1], verts, memo) + lasthoplen
        memo[tuple(path)] = pathlen
        return pathlen

    tissots = []
    allcenters = []
    for hem in ["lh", "rh"]:
        fidvert, fidtri, etc = surfs.getVTK(sub, "fiducial", hem)
        G = make_surface_graph(fidtri)
        nvert = fidvert.shape[0]
        tissot_array = np.zeros((nvert,))

        #maxfails = 20
        numfails = 0
        cnum = 0
        centers = []
        while numfails < maxfails:
            ## Pick random vertex
            centervert = np.random.randint(nvert)
            print "Trying vertex %d.." % centervert

            ## Find distance from this center to all previous centers
            #center_dists = [get_path_len_mm(nx.algorithms.shortest_path(G, centervert, cv), fidvert)
            #                for cv in centers]

            ## Check whether new center is sufficiently far from previous
            paths = nx.algorithms.single_source_shortest_path(G, centervert, spacing/2 + 20)
            scrap = False
            for cv in centers:
                if cv in paths:
                    center_dist = get_path_len_mm(paths[cv], fidvert)
                    if center_dist < spacing:
                        scrap = True
                        break

            if scrap:
                numfails += 1
                print "Vertex too close to others, scrapping (nfails=%d).." % numfails
                continue

            print "Vertex is good center, continuing.."
            centers.append(centervert)
            numfails = 0

            paths = nx.algorithms.single_source_shortest_path(G, centervert, radius*2)
            ## Find all vertices within a few steps of the center
            #nearbyverts = dilate_vertset(set([centervert]), G, radius/2 + 10)

            ## Pull out small graph containing only these vertices
            #subG = G.subgraph(nearbyverts)

            ## Find distance from center to each vertex
            #paths = nx.algorithms.single_source_shortest_path(subG, centervert)
            mymemo = dict()
            dists = dict([(vi,memo_get_path_len_mm(p, fidvert, mymemo)) for vi,p in paths.iteritems()])

            ## Find appropriate set of vertices
            distfun = lambda d: np.tanh(radius-d)/2 + 0.5
            #selverts = np.array([vi for vi,d in dists.iteritems() if d<radius])
            #tissot_array[selverts] = cnum
            verts, vals = map(np.array, zip(*[(vi,distfun(d)) for vi,d in dists.iteritems()]))
            tissot_array[verts] += vals
            print vals.sum()
            cnum += 1

        tissots.append(tissot_array)
        allcenters.append(np.array(centers))

    return tissots, allcenters
