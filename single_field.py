import dolfin as df
from dolfin.function.expression import UserExpression
import numpy as np
import nodal_tools as nt
import time
import os
import sys
from mpi4py import MPI
from shutil import copyfile 

df.parameters['linear_algebra_backend'] = 'PETSc'
df.parameters['form_compiler']['representation'] = 'uflacs'
df.parameters['form_compiler']['optimize'] = True
df.parameters['form_compiler']['cpp_optimize'] = True
df.parameters['form_compiler']['quadrature_degree'] = 3
df.parameters['krylov_solver']['absolute_tolerance' ]= 1E-8
df.parameters['krylov_solver']['relative_tolerance'] = 1E-4
df.parameters['krylov_solver']['maximum_iterations'] = 100000

def main():
    params = {}

    # Mesh and initial condition
    cell = "finger2"
    params['mesh'] = "./cell_data/"+cell+"/predicted_normal.xdmf"
    params['domains'] = "./cell_data/"+cell+"/predicted_normal_domains.xdmf"
    params['boundaries'] = "./cell_data/"+cell+"/predicted_normal_boundaries.xdmf"

    params['mesh_init'] = "./cell_data/"+cell+"/predicted_normal.xdmf"
    # params['u_init'] = "./output/"+cell+"/homogeneous/u_out.xdmf"

    # Material Parameters
    params['mu'] = 100e-6
    params['nu'] = 0.49

    # Boundary Conditions
    params['surface_nodes'] = np.loadtxt("./cell_data/"+cell+"/predicted_normal_vertices_1000.txt")
    params['surface_nodes2'] = np.loadtxt("./cell_data/"+cell+"/CytoD_vertices_1000.txt")
    params['surface_faces'] = np.loadtxt("./cell_data/"+cell+"/CytoD_faces_1000.txt")
    # params['displacements'] = np.loadtxt("./cell_data/"+cell+"/surface_displacements_coarse.txt")

    # Simulation and output
    params['chunks'] = 4
    params['output_folder'] = "./output/"+cell+"/homogeneous"

    solver_call(params)

def solver_call(params):
    # MPI
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    if comm.Get_size()>1:
        df.set_log_level(40)  # Mute output

    # Cell Surface
    surface_nodes = params['surface_nodes']
    surface_faces = params['surface_faces']
    # displacements = params['displacements']
    displacements = params['surface_nodes2'] - params['surface_nodes']

    # Gel Volume Mesh
    mesh = df.Mesh()
    with df.XDMFFile(params["mesh"]) as infile:
        infile.read(mesh)

    # mvc = df.MeshValueCollection("size_t", mesh, 3)
    # with df.XDMFFile(params["domains"]) as infile:
    #     infile.read(mvc, "domains") 
    # domains = df.cpp.mesh.MeshFunctionSizet(mesh, mvc)

    mvc = df.MeshValueCollection("size_t", mesh, 2)
    with df.XDMFFile(params["boundaries"]) as infile:
        infile.read(mvc, "boundaries") 
    boundaries = df.cpp.mesh.MeshFunctionSizet(mesh, mvc)

    # Initialization Mesh
    # mesh_init = df.Mesh()
    # with df.XDMFFile(params["mesh_init"]) as infile:
    #     infile.read(mesh_init)

    # Measures
    dx = df.Measure("dx", domain=mesh)
    ds = df.Measure("ds", domain=mesh, subdomain_data=boundaries)

    # Function Space
    element_u = df.VectorElement("CG", mesh.ufl_cell(), 2)
    V = df.FunctionSpace(mesh, element_u)
    u = df.Function(V)
    u.rename('u','displacement')
    u_ = df.TestFunction(V)
    du = df.TrialFunction(V)

    # Initialize  
    # V_init = df.VectorFunctionSpace(mesh_init, "CG", 2)
    # u_init = df.Function(V_init)  
    # u_init_file = df.XDMFFile(params["u_init"])
    # u_init_file.read_checkpoint(u_init, "u", 0)
    # u_init.set_allow_extrapolation(True)

    # u_0 = df.interpolate(u_init, V)
    # df.assign(u, u_0)

    # Kinematics
    B = df.Constant((0, 0, 0))     # Body force per unit volume
    T = df.Constant((0, 0, 0))     # Traction force on the boundary
    d = u.geometric_dimension()
    I = df.Identity(d)             # Identity tensor
    F = I + df.grad(u)             # Deformation gradient
    Ju = df.det(F)                 # Jacobian
    C = F.T*F                      # Right Cauchy-Green tensor
    C_bar = C/Ju**(2/d)            # Isochoric decomposition
    IC_bar = df.tr(C_bar)          # Invariant

    # Material parameters
    mu_ff = params["mu"]
    nu_ff = params["nu"]
    kappa_ff = 2*mu_ff*(1+nu_ff)/3/(1-2*nu_ff)
    
    c1 = df.Constant(mu_ff/2)
    c2 = df.Constant(kappa_ff)

    # Stored strain energy density (Neo-Hookean formulation)
    psi = c1*(IC_bar-d) + c2*(Ju**2-1-2*df.ln(Ju))/4 

    # Total potential energy
    Pi = psi*dx - df.dot(B, u)*dx - df.dot(T, u)*ds

    # Compute first variation of Pi (directional derivative about u in the direction of w)
    dPi = df.derivative(Pi, u, u_)
    ddPi = df.derivative(dPi, u, du)

    # Boundary Conditions
    midpoints = nt.get_midpoints(surface_nodes, surface_faces)
    midpoint_disp = nt.get_midpoint_disp(displacements, surface_faces)
    face_map = nt.get_face_mapping(midpoints, mesh, boundaries, 202)
    face2disp = dict(zip(face_map, midpoint_disp))

    zero = df.Constant((0.0, 0.0, 0.0))
    bf = nt.BoundaryFunc(mesh, face2disp, 0)

    outer_bc = df.DirichletBC(V, zero, boundaries, 201)
    inner_bc = df.DirichletBC(V, bf, boundaries, 202)
    bcs = [inner_bc, outer_bc]

    # Create nonlinear variational problem
    problem = df.NonlinearVariationalProblem(dPi, u, bcs=bcs, J=ddPi)
    solver = df.NonlinearVariationalSolver(problem)
    solver.parameters['newton_solver']['linear_solver']  = 'gmres'
    solver.parameters['newton_solver']['preconditioner']  = 'hypre_amg'
    # solver.parameters['newton_solver']['relative_tolerance'] = 1e-5

    # MPI
    ele = np.array(len(mesh.cells()),'d') # Number of elements
    ele_sum = np.array(0.,'d')
    comm.Reduce(ele, ele_sum, op=MPI.SUM, root=0)

    output_folder = params["output_folder"]
    if rank==0:
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

        print("Mesh: ", params["mesh"])
        print('Total number of elements = {:d}'.format(int(ele_sum)))
        print("Solving =========================")

    # Solve
    chunks = params["chunks"]
    u.vector()[:]*=1/chunks

    sys.stdout.flush()
    total_start = time.time() 
    for i in range(chunks):
        start = time.time()
        if rank == 0: print("    Iter: ", i)
        sys.stdout.flush()  

        bf.scalar = (i+1)/chunks
        solver.solve()

        end = time.time()
        time_elapsed = end - start
        if rank == 0: print('    Time elapsed = {:2.1f}s'.format(time_elapsed))
        sys.stdout.flush()  

    # Projections
    F = df.project(F, V=df.TensorFunctionSpace(mesh, "CG", 1, shape=(3, 3)), solver_type = 'cg', preconditioner_type = 'amg')
    J = df.project(Ju, V=df.FunctionSpace(mesh, "DG", 0))
    mu = df.project(mu_ff, V=df.FunctionSpace(mesh, "CG", 1))

    # Outputs
    output_folder = params["output_folder"]
    if rank==0:
        if not os.path.exists(output_folder):
            os.makedirs(output_folder)

    u_file = df.XDMFFile(os.path.join(output_folder, "u.xdmf"))
    u.rename("u","displacement")
    u_file.write(u)

    F_file = df.XDMFFile(os.path.join(output_folder, "F.xdmf"))
    F.rename("F","deformation gradient")
    F_file.write(F)

    J_file = df.XDMFFile(os.path.join(output_folder, "J.xdmf"))
    J.rename("J","Jacobian")
    J_file.write(J)

    mu_file = df.XDMFFile(os.path.join(output_folder, "mu.xdmf"))
    mu.rename("mu","shear modulus")
    mu_file.write(mu)

    out_file = df.XDMFFile((os.path.join(output_folder, "u_out.xdmf")))
    out_file.write_checkpoint(u, "u", 0)   #Not appending

    if rank==0:
        python_file = os.path.basename(__file__)
        copyfile(python_file, os.path.join(output_folder, python_file))

        with open(os.path.join(output_folder,"log_params.txt"), "w+") as f:
            f.write("Mesh: {:s}\n".format(params["mesh"]))
            f.write("No. Elements: {:d}\n".format(int(ele_sum)))
            f.write("No. Processors: {:d}\n".format(int(comm.Get_size())))
            f.write("mu_ff = {:e}\n".format(mu_ff))
            f.write("nu_ff = {:e}\n".format(nu_ff))
            f.write("Total Time = {:f}s\n".format(time.time()-total_start))

        print("Done")

if __name__=="__main__":
    main()
