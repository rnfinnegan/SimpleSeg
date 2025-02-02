"""
Provides tools to perform label fusion for use within atlas based segmentation algorithms.
"""

from functools import reduce

import SimpleITK as sitk
import numpy as np
import itk

def sitk_to_itk(sitk_image):
    """
    Helper function to convert SimpleITK images to ITK images
    """
    sitk_arr = sitk.GetArrayFromImage(sitk_image)

    itk_image = itk.GetImageFromArray(sitk_arr, is_vector = False)
    itk_image.SetOrigin(sitk_image.GetOrigin())
    itk_image.SetSpacing(sitk_image.GetSpacing())
    itk_image.SetDirection(itk.GetMatrixFromArray(np.reshape(np.array(sitk_image.GetDirection()), [3]*2)))

    return itk_image

def itk_to_sitk(itk_image):
    """
    Helper function to convert ITK images to SimpleITK images
    """
    sitk_image = sitk.GetImageFromArray(itk.GetArrayFromImage(itk_image), isVector=False)
    sitk_image.SetOrigin(tuple(itk_image.GetOrigin()))
    sitk_image.SetSpacing(tuple(itk_image.GetSpacing()))
    sitk_image.SetDirection(itk.GetArrayFromMatrix(itk_image.GetDirection()).flatten())

    return sitk_image

def morphological_interpolate(sitk_image):
    """
    Performs morphological interpolation
    See: https://github.com/KitwareMedical/ITKMorphologicalContourInterpolation

    Useful for filling in gaps in contouring between slices
    """

    itk_image = sitk_to_itk(sitk_image)

    output_type = itk.Image[itk.UC, 3]

    f_cast = itk.CastImageFilter[itk_image, output_type].New()
    f_cast.SetInput(itk_image)
    img_cast = f_cast.GetOutput()

    f_interpolator = itk.MorphologicalContourInterpolator.New()
    f_interpolator.SetInput(img_cast)
    f_interpolator.Update()

    img_interpolated = f_interpolator.GetOutput()

    sitk_img_interpolated = itk_to_sitk(img_interpolated)

    return sitk_img_interpolated

def compute_weight_map(
    target_image,
    moving_image,
    vote_type="local",
    vote_params={"sigma": 2.0, "epsilon": 1e-5, "factor": 1e12, "gain": 6, "blockSize": 5},
):
    """
    Computes the weight map
    """

    # Cast to floating point representation, if necessary
    if target_image.GetPixelID() != 6:
        target_image = sitk.Cast(target_image, sitk.sitkFloat32)
    if moving_image.GetPixelID() != 6:
        moving_image = sitk.Cast(moving_image, sitk.sitkFloat32)

    square_difference_image = sitk.SquaredDifference(target_image, moving_image)
    square_difference_image = sitk.Cast(square_difference_image, sitk.sitkFloat32)

    if vote_type.lower() == "unweighted":
        weight_map = target_image * 0.0 + 1.0

    elif vote_type.lower() == "global":
        factor = vote_params["factor"]
        sum_squared_difference = sitk.GetArrayFromImage(square_difference_image).sum(
            dtype=np.float
        )
        global_weight = factor / sum_squared_difference

        weight_map = target_image * 0.0 + global_weight

    elif vote_type.lower() == "local":
        sigma = vote_params["sigma"]
        epsilon = vote_params["epsilon"]

        raw_map = sitk.DiscreteGaussian(square_difference_image, sigma * sigma)
        weight_map = sitk.Pow(raw_map + epsilon, -1.0)

    elif vote_type.lower() == "block":
        factor = vote_params["factor"]
        gain = vote_params["gain"]
        block_size = vote_params["blockSize"]
        if isinstance(block_size, int):
            block_size = (block_size,) * target_image.GetDimension()

        # rawMap = sitk.Mean(square_difference_image, blockSize)
        raw_map = sitk.BoxMean(square_difference_image, block_size)
        weight_map = factor * sitk.Pow(raw_map, -1.0) ** abs(gain / 2.0)
        # Note: we divide gain by 2 to account for using the squared difference image
        #       which raises the power by 2 already.

    else:
        raise ValueError("Weighting scheme not valid.")

    return sitk.Cast(weight_map, sitk.sitkFloat32)


def combine_labels_staple(label_list_dict, threshold=1e-4):
    """
    Combine labels using STAPLE
    """

    combined_label_dict = {}

    structure_name_list = [list(i.keys()) for i in label_list_dict.values()]
    structure_name_list = np.unique([item for sublist in structure_name_list for item in sublist])

    for structure_name in structure_name_list:
        # Ensure all labels are binarised
        binary_labels = [
            sitk.BinaryThreshold(label_list_dict[i][structure_name], lowerThreshold=0.5)
            for i in label_list_dict
        ]

        # Perform STAPLE
        combined_label = sitk.STAPLE(binary_labels)

        # Normalise
        combined_label = sitk.RescaleIntensity(combined_label, 0, 1)

        # Threshold - grants vastly improved compression performance
        if threshold:
            combined_label = sitk.Threshold(
                combined_label, lower=threshold, upper=1, outsideValue=0.0
            )

        combined_label_dict[structure_name] = combined_label

    return combined_label_dict


def combine_labels(atlas_set, structure_name, threshold=1e-4, smooth_sigma=1.0):
    """
    Combine labels using weight maps
    """

    case_id_list = list(atlas_set.keys())

    if isinstance(structure_name, str):
        structure_name_list = [structure_name]
    elif isinstance(structure_name, list):
        structure_name_list = structure_name

    combined_label_dict = {}

    for structure_name in structure_name_list:
        # Find the cases which have the strucure (in case some cases do not)
        valid_case_id_list = [
            i for i in case_id_list if structure_name in atlas_set[i]["DIR"].keys()
        ]

        # Get valid weight images
        weight_image_list = [
            atlas_set[caseId]["DIR"]["Weight Map"] for caseId in valid_case_id_list
        ]

        # Sum the weight images
        weight_sum_image = sum( weight_image_list )
        weight_sum_image = sitk.Mask(
            weight_sum_image, weight_sum_image == 0, maskingValue=1, outsideValue=1
        )

        # Combine weight map with each label
        weighted_labels = [
            atlas_set[caseId]["DIR"]["Weight Map"]
            * sitk.Cast(atlas_set[caseId]["DIR"][structure_name], sitk.sitkFloat32)
            for caseId in valid_case_id_list
        ]

        # Combine all the weighted labels
        combined_label = reduce(lambda x, y: x + y, weighted_labels) / weight_sum_image

        # Smooth combined label
        combined_label = sitk.DiscreteGaussian(combined_label, smooth_sigma * smooth_sigma)

        # Normalise
        combined_label = sitk.RescaleIntensity(combined_label, 0, 1)

        # Threshold - grants vastly improved compression performance
        if threshold:
            combined_label = sitk.Threshold(
                combined_label, lower=threshold, upper=1, outsideValue=0.0
            )

        combined_label_dict[structure_name] = combined_label

    return combined_label_dict


def process_probability_image(probability_image, threshold=0.5):
    """
    Generate a mask given a probability image, performing some basic post processing as well.
    """

    # Check type
    if not isinstance(probability_image, sitk.Image):
        probability_image = sitk.GetImageFromArray(probability_image)

    # Normalise probability map
    probability_image = (probability_image / sitk.GetArrayFromImage(probability_image).max())

    # Get the starting binary image
    binary_image = sitk.BinaryThreshold(probability_image, lowerThreshold=threshold)

    # Fill holes
    binary_image = sitk.BinaryFillhole(binary_image)

    # Apply the connected component filter
    labelled_image = sitk.ConnectedComponent(binary_image)

    # Measure the size of each connected component
    label_shape_filter = sitk.LabelShapeStatisticsImageFilter()
    label_shape_filter.Execute(labelled_image)
    label_indices = label_shape_filter.GetLabels()
    voxel_counts = [label_shape_filter.GetNumberOfPixels(i) for i in label_indices]
    if voxel_counts == []:
        return binary_image

    # Select the largest region
    largest_component_label = label_indices[np.argmax(voxel_counts)]
    largest_component_image = (labelled_image == largest_component_label)

    return sitk.Cast(largest_component_image, sitk.sitkUInt8)
