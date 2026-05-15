# Copyright (c) Meta Platforms, Inc. and affiliates.
from typing import *
import numpy as np
import torch
import utils3d

# ── utils3d 1.7 API ──────────────────────────────────────────────────────────
from utils3d.torch.transforms import perspective_from_fov as _persp_fov17
from utils3d.torch.rasterization import rasterize_triangles as _rast17
from utils3d.torch.mesh import remove_unused_vertices as _remove_verts17
# ─────────────────────────────────────────────────────────────────────────────
from PIL import Image
from tqdm import tqdm
import trimesh
import trimesh.visual
import xatlas
import pyvista as pv
from pymeshfix import _meshfix
import cv2
from PIL import Image
from .random_utils import sphere_hammersley_sequence
from .render_utils import render_multiview
from ..renderers import GaussianRenderer
from ..representations import Strivec, Gaussian, MeshExtractResult
from loguru import logger

@torch.no_grad()
def _fill_holes(
    verts,
    faces,
    max_hole_nbe=32,
    resolution=128,
    num_views=500,
    verbose=False,
):
    """
    Rasterize a mesh from multiple views and remove invisible faces.
    Also includes postprocessing to:
        1. Remove connected components that are have low visibility.
        2. Mincut to remove faces at the inner side of the mesh connected to the outer side with a small hole.

    Args:
        verts (torch.Tensor): Vertices of the mesh. Shape (V, 3).
        faces (torch.Tensor): Faces of the mesh. Shape (F, 3).
        max_hole_size (float): Maximum area of a hole to fill.
        resolution (int): Resolution of the rasterization.
        num_views (int): Number of views to rasterize the mesh.
        verbose (bool): Whether to print progress.
    """
    # Construct cameras
    yaws = []
    pitchs = []
    for i in range(num_views):
        y, p = sphere_hammersley_sequence(i, num_views)
        yaws.append(y)
        pitchs.append(p)
    yaws = torch.tensor(yaws).cuda()
    pitchs = torch.tensor(pitchs).cuda()
    radius = 2.0
    fov = torch.deg2rad(torch.tensor(40)).cuda()
    projection = _persp_fov17(fov_x=fov, fov_y=fov, near=1, far=3)
    views = []
    for yaw, pitch in zip(yaws, pitchs):
        orig = (
            torch.tensor(
                [
                    torch.sin(yaw) * torch.cos(pitch),
                    torch.cos(yaw) * torch.cos(pitch),
                    torch.sin(pitch),
                ]
            )
            .cuda()
            .float()
            * radius
        )
        view = utils3d.torch.view_look_at(
            orig,
            torch.tensor([0, 0, 0]).float().cuda(),
            torch.tensor([0, 0, 1]).float().cuda(),
        )
        views.append(view)
    views = torch.stack(views, dim=0)

    # Rasterize
    visblity = torch.zeros(faces.shape[0], dtype=torch.int32, device=verts.device)
    rastctx = utils3d.torch.RastContext(backend="cuda")
    for i in tqdm(
        range(views.shape[0]),
        total=views.shape[0],
        disable=not verbose,
        desc="Rasterizing",
    ):
        view = views[i]
        buffers = _rast17(
            (resolution, resolution),
            vertices=verts[None],
            faces=faces,
            view=view,
            projection=projection,
            return_interpolation=True,
            ctx=rastctx,
        )
        face_id = buffers["interpolation_id"][0][buffers["mask"][0]]
        face_id = torch.unique(face_id[face_id >= 0]).long()
        visblity[face_id] += 1
    visblity = visblity.float() / num_views

    # Skip igraph mincut — remove invisible faces directly and fill holes
    invisible_mask = visblity > 0
    faces = faces[invisible_mask]
    if faces.shape[0] > 0:
        faces, verts = _remove_verts17(faces, verts)
    mesh = _meshfix.PyTMesh()
    mesh.load_array(verts.cpu().numpy(), faces.cpu().numpy())
    mesh.fill_small_boundaries(nbe=max_hole_nbe, refine=True)
    verts, faces = mesh.return_arrays()
    verts = torch.tensor(verts, device="cuda", dtype=torch.float32)
    faces = torch.tensor(faces, device="cuda", dtype=torch.int32)
    return verts, faces


def postprocess_mesh(
    vertices: np.array,
    faces: np.array,
    simplify: bool = True,
    simplify_ratio: float = 0.9,
    fill_holes: bool = True,
    fill_holes_max_hole_nbe: int = 32,
    fill_holes_resolution: int = 1024,
    fill_holes_num_views: int = 1000,
    verbose: bool = False,
):
    """
    Postprocess a mesh by simplifying, removing invisible faces, and removing isolated pieces.

    Args:
        vertices (np.array): Vertices of the mesh. Shape (V, 3).
        faces (np.array): Faces of the mesh. Shape (F, 3).
        simplify (bool): Whether to simplify the mesh, using quadric edge collapse.
        simplify_ratio (float): Ratio of faces to keep after simplification.
        fill_holes (bool): Whether to fill holes in the mesh.
        fill_holes_max_hole_size (float): Maximum area of a hole to fill.
        fill_holes_max_hole_nbe (int): Maximum number of boundary edges of a hole to fill.
        fill_holes_resolution (int): Resolution of the rasterization.
        fill_holes_num_views (int): Number of views to rasterize the mesh.
        verbose (bool): Whether to print progress.
    """

    if verbose:
        tqdm.write(
            f"Before postprocess: {vertices.shape[0]} vertices, {faces.shape[0]} faces"
        )

    # Simplify
    if simplify and simplify_ratio > 0:
        mesh = pv.PolyData(
            vertices, np.concatenate([np.full((faces.shape[0], 1), 3), faces], axis=1)
        )
        mesh = mesh.decimate(simplify_ratio, progress_bar=verbose)
        vertices, faces = mesh.points, mesh.faces.reshape(-1, 4)[:, 1:]
        if verbose:
            tqdm.write(
                f"After decimate: {vertices.shape[0]} vertices, {faces.shape[0]} faces"
            )

    # Remove invisible faces
    if fill_holes:
        vertices, faces = (
            torch.tensor(vertices).cuda(),
            torch.tensor(faces.astype(np.int32)).cuda(),
        )
        vertices, faces = _fill_holes(
            vertices,
            faces,
            max_hole_nbe=fill_holes_max_hole_nbe,
            resolution=fill_holes_resolution,
            num_views=fill_holes_num_views,
            verbose=verbose,
        )
        vertices, faces = vertices.cpu().numpy(), faces.cpu().numpy()
        if verbose:
            tqdm.write(
                f"After remove invisible faces: {vertices.shape[0]} vertices, {faces.shape[0]} faces"
            )

    return vertices, faces


def parametrize_mesh(vertices: np.array, faces: np.array):
    """
    Parametrize a mesh to a texture space, using xatlas.

    Args:
        vertices (np.array): Vertices of the mesh. Shape (V, 3).
        faces (np.array): Faces of the mesh. Shape (F, 3).
    """

    vmapping, indices, uvs = xatlas.parametrize(vertices, faces)

    vertices = vertices[vmapping]
    faces = indices

    return vertices, faces, uvs

@torch.inference_mode(False)
@torch.enable_grad()
def bake_texture(
    vertices: np.array,
    faces: np.array,
    uvs: np.array,
    observations: List[np.array],
    masks: List[np.array],
    extrinsics: List[np.array],
    intrinsics: List[np.array],
    texture_size: int = 2048,
    near: float = 0.1,
    far: float = 10.0,
    mode: Literal["fast", "opt"] = "opt",
    lambda_tv: float = 1e-2,
    verbose: bool = False,
    rendering_engine: str = "nvdiffrast",  # nvdiffrast OR "pytorch3d"
    device: str = "cuda",

):
    """
    Bake texture to a mesh from multiple observations.

    Args:
        vertices (np.array): Vertices of the mesh. Shape (V, 3).
        faces (np.array): Faces of the mesh. Shape (F, 3).
        uvs (np.array): UV coordinates of the mesh. Shape (V, 2).
        observations (List[np.array]): List of observations. Each observation is a 2D image. Shape (H, W, 3).
        masks (List[np.array]): List of masks. Each mask is a 2D image. Shape (H, W).
        extrinsics (List[np.array]): List of extrinsics. Shape (4, 4).
        intrinsics (List[np.array]): List of intrinsics. Shape (3, 3).
        texture_size (int): Size of the texture.
        near (float): Near plane of the camera.
        far (float): Far plane of the camera.
        mode (Literal['fast', 'opt']): Mode of texture baking.
        lambda_tv (float): Weight of total variation loss in optimization.
        verbose (bool): Whether to print progress.
    """


    vertices = torch.tensor(vertices).to(device)
    faces = torch.tensor(faces.astype(np.int32)).to(device)
    uvs = torch.tensor(uvs).to(device)
    observations = [torch.tensor(obs / 255.0).float().to(device) for obs in observations]
    masks = [torch.tensor(m > 0).bool().to(device) for m in masks]
    views = [
        utils3d.torch.extrinsics_to_view(torch.tensor(extr).to(device))
        for extr in extrinsics
    ]
    projections = [
        utils3d.torch.intrinsics_to_perspective(torch.tensor(intr).to(device), near, far)
        for intr in intrinsics
    ]

    if mode == "fast":
        texture = torch.zeros(
            (texture_size * texture_size, 3), dtype=torch.float32
        ).to(device)
        texture_weights = torch.zeros(
            (texture_size * texture_size), dtype=torch.float32
        ).to(device)
        rastctx = utils3d.torch.RastContext(backend=device if device.startswith("cuda") else "cuda")
        for observation, view, projection in tqdm(
            zip(observations, views, projections),
            total=len(observations),
            disable=not verbose,
            desc="Texture baking (fast)",
        ):
            with torch.no_grad():
                rast = utils3d.torch.rasterize_triangle_faces(
                    rastctx,
                    vertices[None],
                    faces,
                    observation.shape[1],
                    observation.shape[0],
                    uv=uvs[None],
                    view=view,
                    projection=projection,
                )
                uv_map = rast["uv"][0].detach().flip(0)
                mask = rast["mask"][0].detach().bool() & masks[0]

            # nearest neighbor interpolation
            uv_map = (uv_map * texture_size).floor().long()
            obs = observation[mask]
            uv_map = uv_map[mask]
            idx = uv_map[:, 0] + (texture_size - uv_map[:, 1] - 1) * texture_size
            texture = texture.scatter_add(0, idx.view(-1, 1).expand(-1, 3), obs)
            texture_weights = texture_weights.scatter_add(
                0,
                idx,
                torch.ones((obs.shape[0]), dtype=torch.float32, device=texture.device),
            )

        mask = texture_weights > 0
        texture[mask] /= texture_weights[mask][:, None]
        texture = np.clip(
            texture.reshape(texture_size, texture_size, 3).cpu().numpy() * 255, 0, 255
        ).astype(np.uint8)

        # inpaint
        mask = (
            (texture_weights == 0)
            .cpu()
            .numpy()
            .astype(np.uint8)
            .reshape(texture_size, texture_size)
        )
        texture = cv2.inpaint(texture, mask, 3, cv2.INPAINT_TELEA)

    elif mode == "opt":
        rastctx = utils3d.torch.RastContext(backend=device if device.startswith("cuda") else "cuda")
        observations = [observations.flip(0) for observations in observations]
        masks = [m.flip(0) for m in masks]
        _uv = []
        _uv_dr = []
        for observation, view, projection in tqdm(
            zip(observations, views, projections),
            total=len(views),
            disable=not verbose,
            desc="Texture baking (opt): UV",
        ):
            with torch.no_grad():
                rast = utils3d.torch.rasterize_triangle_faces(
                    rastctx,
                    vertices[None],
                    faces,
                    observation.shape[1],
                    observation.shape[0],
                    uv=uvs[None],
                    view=view,
                    projection=projection,
                )
                _uv.append(rast["uv"].detach())
                _uv_dr.append(rast["uv_dr"].detach())

        texture = torch.nn.Parameter(
            torch.zeros((1, texture_size, texture_size, 3), dtype=torch.float32).to(device)
        )
        optimizer = torch.optim.Adam([texture], betas=(0.5, 0.9), lr=1e-2)

        def exp_anealing(optimizer, step, total_steps, start_lr, end_lr):
            return start_lr * (end_lr / start_lr) ** (step / total_steps)

        def cosine_anealing(optimizer, step, total_steps, start_lr, end_lr):
            return end_lr + 0.5 * (start_lr - end_lr) * (
                1 + np.cos(np.pi * step / total_steps)
            )

        def tv_loss(texture):
            return torch.nn.functional.l1_loss(
                texture[:, :-1, :, :], texture[:, 1:, :, :]
            ) + torch.nn.functional.l1_loss(texture[:, :, :-1, :], texture[:, :, 1:, :])



        def render_pt3d_texture(texture, uv, uv_dr=None):
            import torch.nn.functional as F
            texture_perm = texture.permute(0, 3, 1, 2)
            grid = uv * 2 - 1
            if grid.dim() == 3:
                grid = grid.unsqueeze(0)  # (1, H, W, 2)
            elif grid.dim() == 4 and grid.shape[0] == 1:
                pass  
            elif grid.dim() == 4 and grid.shape[1] == 1:
                grid = grid.squeeze(1)  # remove extra batch dimension if necessary
            else:
                raise ValueError(f"Unexpected grid shape: {grid.shape}")
            render = F.grid_sample(
                texture_perm, grid, mode='bilinear', padding_mode='border', align_corners=True
            )
            render = render.permute(0, 2, 3, 1)[0]  # (H_out, W_out, 3)
            return render
        
        
        total_steps = 2500
        
        with tqdm(
            total=total_steps,
            disable=not verbose,
            desc="Texture baking (opt): optimizing",
            ) as pbar:
            for step in range(total_steps):
                optimizer.zero_grad()
                selected = np.random.randint(0, len(views))
                uv, uv_dr, observation, mask = (
                    _uv[selected],
                    _uv_dr[selected],
                    observations[selected],
                    masks[selected],
                )
                
                if rendering_engine == "nvdiffrast":
                    import nvdiffrast.torch as dr
                    render = dr.texture(texture, uv, uv_dr)[0]

                if rendering_engine == "pytorch3d":
                    render = render_pt3d_texture(texture, uv)
                    
                loss = torch.nn.functional.l1_loss(render[mask], observation[mask])
                if lambda_tv > 0:
                    loss += lambda_tv * tv_loss(texture)
                loss.backward()
                optimizer.step()
                # annealing
                optimizer.param_groups[0]["lr"] = cosine_anealing(
                    optimizer, step, total_steps, 1e-2, 1e-5
                    )
                pbar.set_postfix({"loss": loss.item()})
                pbar.update()
        texture = np.clip(
            texture[0].flip(0).detach().cpu().numpy() * 255, 0, 255
        ).astype(np.uint8)
        mask = 1 - utils3d.torch.rasterize_triangle_faces(
            rastctx, (uvs * 2 - 1)[None], faces, texture_size, texture_size
        )["mask"][0].detach().cpu().numpy().astype(np.uint8)
        texture = cv2.inpaint(texture, mask, 3, cv2.INPAINT_TELEA)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    return texture


def to_glb(
    app_rep: Union[Strivec, Gaussian],
    mesh: MeshExtractResult,
    simplify: float = 0.95,
    fill_holes: bool = True,
    texture_size: int = 1024,
    verbose: bool = True,
    with_mesh_postprocess=True,
    with_texture_baking=True,
    use_vertex_color=False,
    rendering_engine: str = "nvdiffrast",  # nvdiffrast OR "pytorch3d"
) -> trimesh.Trimesh:
    """
    Convert a generated asset to a glb file.

    Args:
        app_rep (Union[Strivec, Gaussian]): Appearance representation.
        mesh (MeshExtractResult): Extracted mesh.
        simplify (float): Ratio of faces to remove in simplification.
        fill_holes (bool): Whether to fill holes in the mesh.
        fill_holes_max_size (float): Maximum area of a hole to fill.
        texture_size (int): Size of the texture.
        debug (bool): Whether to print debug information.
        verbose (bool): Whether to print progress.
    """
    vertices = mesh.vertices.float().cpu().numpy()
    faces = mesh.faces.cpu().numpy()
    vert_colors = mesh.vertex_attrs[:, :3].cpu().numpy()

    orig_vertices_for_color = vertices.copy()

    if with_mesh_postprocess:
        # mesh postprocess
        vertices, faces = postprocess_mesh(
            vertices,
            faces,
            simplify=simplify > 0,
            simplify_ratio=simplify,
            fill_holes=fill_holes,
            fill_holes_max_hole_nbe=int(250 * np.sqrt(1 - simplify)),
            fill_holes_resolution=128,
            fill_holes_num_views=50,
            verbose=verbose,
        )

    if with_texture_baking:
        # parametrize mesh
        vertices, faces, uvs = parametrize_mesh(vertices, faces)
        logger.info("Baking texture ...")

        # bake texture
        observations, extrinsics, intrinsics = render_multiview(
            app_rep, resolution=1024, nviews=100
        )
        masks = [np.any(observation > 0, axis=-1) for observation in observations]
        extrinsics = [extrinsics[i].cpu().numpy() for i in range(len(extrinsics))]
        intrinsics = [intrinsics[i].cpu().numpy() for i in range(len(intrinsics))]
        texture = bake_texture(
            vertices,
            faces,
            uvs,
            observations,
            masks,
            extrinsics,
            intrinsics,
            texture_size=texture_size,
            mode="opt",
            lambda_tv=0.01,
            verbose=verbose,
            rendering_engine=rendering_engine
        )
        texture = Image.fromarray(texture)
        material = trimesh.visual.material.PBRMaterial(
            roughnessFactor=1.0,
            baseColorTexture=texture,
            baseColorFactor=np.array([255, 255, 255, 255], dtype=np.uint8),
        )

    rot = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    vertices = vertices @ rot

    if with_texture_baking:
        mesh = trimesh.Trimesh(
            vertices, faces,
            visual=trimesh.visual.TextureVisuals(uv=uvs, material=material),
        )
    elif use_vertex_color:
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        n_new, n_orig = vertices.shape[0], vert_colors.shape[0]
        if n_new == n_orig:
            colors = vert_colors
        else:
            # 포스트프로세스로 버텍스 수 달라짐 → 원본 버텍스 KDTree로 색상 전이
            from scipy.spatial import cKDTree
            orig_rotated = orig_vertices_for_color @ rot
            _, idx = cKDTree(orig_rotated).query(vertices)
            colors = vert_colors[idx]
        mesh.visual.vertex_colors = colors
    else:
        mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)

    return mesh


def simplify_gs(
    gs: Gaussian,
    simplify: float = 0.95,
    verbose: bool = True,
):
    """
    Simplify 3D Gaussians
    NOTE: this function is not used in the current implementation for the unsatisfactory performance.

    Args:
        gs (Gaussian): 3D Gaussian.
        simplify (float): Ratio of Gaussians to remove in simplification.
    """
    if simplify <= 0:
        return gs

    # simplify
    observations, extrinsics, intrinsics = render_multiview(
        gs, resolution=1024, nviews=100
    )
    observations = [
        torch.tensor(obs / 255.0).float().cuda().permute(2, 0, 1)
        for obs in observations
    ]

    # Following https://arxiv.org/pdf/2411.06019
    renderer = GaussianRenderer(
        {
            "resolution": 1024,
            "near": 0.8,
            "far": 1.6,
            "ssaa": 1,
            "bg_color": (0, 0, 0),
        }
    )
    new_gs = Gaussian(**gs.init_params)
    new_gs._features_dc = gs._features_dc.clone()
    new_gs._features_rest = (
        gs._features_rest.clone() if gs._features_rest is not None else None
    )
    new_gs._opacity = torch.nn.Parameter(gs._opacity.clone())
    new_gs._rotation = torch.nn.Parameter(gs._rotation.clone())
    new_gs._scaling = torch.nn.Parameter(gs._scaling.clone())
    new_gs._xyz = torch.nn.Parameter(gs._xyz.clone())

    start_lr = [1e-4, 1e-3, 5e-3, 0.025]
    end_lr = [1e-6, 1e-5, 5e-5, 0.00025]
    optimizer = torch.optim.Adam(
        [
            {"params": new_gs._xyz, "lr": start_lr[0]},
            {"params": new_gs._rotation, "lr": start_lr[1]},
            {"params": new_gs._scaling, "lr": start_lr[2]},
            {"params": new_gs._opacity, "lr": start_lr[3]},
        ],
        lr=start_lr[0],
    )

    def exp_anealing(optimizer, step, total_steps, start_lr, end_lr):
        return start_lr * (end_lr / start_lr) ** (step / total_steps)

    def cosine_anealing(optimizer, step, total_steps, start_lr, end_lr):
        return end_lr + 0.5 * (start_lr - end_lr) * (
            1 + np.cos(np.pi * step / total_steps)
        )

    _zeta = new_gs.get_opacity.clone().detach().squeeze()
    _lambda = torch.zeros_like(_zeta)
    _delta = 1e-7
    _interval = 10
    num_target = int((1 - simplify) * _zeta.shape[0])

    with tqdm(total=2500, disable=not verbose, desc="Simplifying Gaussian") as pbar:
        for i in range(2500):
            # prune
            if i % 100 == 0:
                mask = new_gs.get_opacity.squeeze() > 0.05
                mask = torch.nonzero(mask).squeeze()
                new_gs._xyz = torch.nn.Parameter(new_gs._xyz[mask])
                new_gs._rotation = torch.nn.Parameter(new_gs._rotation[mask])
                new_gs._scaling = torch.nn.Parameter(new_gs._scaling[mask])
                new_gs._opacity = torch.nn.Parameter(new_gs._opacity[mask])
                new_gs._features_dc = new_gs._features_dc[mask]
                new_gs._features_rest = (
                    new_gs._features_rest[mask]
                    if new_gs._features_rest is not None
                    else None
                )
                _zeta = _zeta[mask]
                _lambda = _lambda[mask]
                # update optimizer state
                for param_group, new_param in zip(
                    optimizer.param_groups,
                    [new_gs._xyz, new_gs._rotation, new_gs._scaling, new_gs._opacity],
                ):
                    stored_state = optimizer.state[param_group["params"][0]]
                    if "exp_avg" in stored_state:
                        stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                        stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]
                    del optimizer.state[param_group["params"][0]]
                    param_group["params"][0] = new_param
                    optimizer.state[param_group["params"][0]] = stored_state

            opacity = new_gs.get_opacity.squeeze()

            # sparisfy
            if i % _interval == 0:
                _zeta = _lambda + opacity.detach()
                if opacity.shape[0] > num_target:
                    index = _zeta.topk(num_target)[1]
                    _m = torch.ones_like(_zeta, dtype=torch.bool)
                    _m[index] = 0
                    _zeta[_m] = 0
                _lambda = _lambda + opacity.detach() - _zeta

            # sample a random view
            view_idx = np.random.randint(len(observations))
            observation = observations[view_idx]
            extrinsic = extrinsics[view_idx]
            intrinsic = intrinsics[view_idx]

            color = renderer.render(new_gs, extrinsic, intrinsic)["color"]
            rgb_loss = torch.nn.functional.l1_loss(color, observation)
            loss = rgb_loss + _delta * torch.sum(
                torch.pow(_lambda + opacity - _zeta, 2)
            )

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # update lr
            for j in range(len(optimizer.param_groups)):
                optimizer.param_groups[j]["lr"] = cosine_anealing(
                    optimizer, i, 2500, start_lr[j], end_lr[j]
                )

            pbar.set_postfix(
                {
                    "loss": rgb_loss.item(),
                    "num": opacity.shape[0],
                    "lambda": _lambda.mean().item(),
                }
            )
            pbar.update()

    new_gs._xyz = new_gs._xyz.data
    new_gs._rotation = new_gs._rotation.data
    new_gs._scaling = new_gs._scaling.data
    new_gs._opacity = new_gs._opacity.data

    return new_gs
