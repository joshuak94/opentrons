// @flow
import { createSelector } from 'reselect'

import isEmpty from 'lodash/isEmpty'
import mapValues from 'lodash/mapValues'
import min from 'lodash/min'
import pick from 'lodash/pick'
import reduce from 'lodash/reduce'
import omitBy from 'lodash/omitBy'
import assert from 'assert'

import type { LabwareDefinition2 } from '@opentrons/shared-data'
import * as StepGeneration from '../../step-generation'
import { selectors as fileDataSelectors } from '../../file-data'
import { selectors as labwareIngredSelectors } from '../../labware-ingred/selectors'
import { selectors as stepFormSelectors } from '../../step-forms'
import { selectors as stepsSelectors } from '../../ui/steps'
import wellSelectionSelectors from '../../well-selection/selectors'
import { START_TERMINAL_ITEM_ID } from '../../steplist'
import { getAllWellsForLabware, getMaxVolumes } from '../../constants'

import type { Selector } from '../../types'
import type {
  WellContents,
  WellContentsByLabware,
  ContentsByWell,
} from '../../labware-ingred/types'

// TODO Ian 2018-04-19: factor out all these selectors to their own files,
// and make this index.js just imports and exports.
import getWellContentsAllLabware from './getWellContentsAllLabware'
export { getWellContentsAllLabware }
export type { WellContentsByLabware }

function _wellContentsForWell(
  liquidVolState: StepGeneration.LocationLiquidState,
  well: string
): WellContents {
  // TODO IMMEDIATELY Ian 2018-03-23 why is liquidVolState missing sometimes (eg first call with trashId)? Thus the liquidVolState || {}
  const ingredGroupIdsWithContent = Object.keys(liquidVolState || {}).filter(
    groupId => liquidVolState[groupId] && liquidVolState[groupId].volume > 0
  )

  return {
    wellName: well,
    groupIds: ingredGroupIdsWithContent, // TODO: BC 2018-09-21 remove in favor of volumeByGroupId
    ingreds: omitBy(
      liquidVolState,
      ingredData => !ingredData || ingredData.volume <= 0
    ),
  }
}

export function _wellContentsForLabware(
  labwareLiquids: StepGeneration.SingleLabwareLiquidState,
  labwareDef: LabwareDefinition2
): ContentsByWell {
  const allWellsForContainer = getAllWellsForLabware(labwareDef)

  return reduce(
    allWellsForContainer,
    (wellAcc, well: string): { [well: string]: WellContents } => {
      const wellHasContents = labwareLiquids && labwareLiquids[well]
      return {
        ...wellAcc,
        [well]: wellHasContents
          ? _wellContentsForWell(labwareLiquids[well], well)
          : {},
      }
    },
    {}
  )
}

export const getAllWellContentsForSteps: Selector<
  Array<WellContentsByLabware>
> = createSelector(
  fileDataSelectors.getInitialRobotState,
  fileDataSelectors.getRobotStateTimeline,
  stepFormSelectors.getLabwareEntities,
  (initialRobotState, robotStateTimeline, labwareEntities) => {
    const timeline = [
      { robotState: initialRobotState },
      ...robotStateTimeline.timeline,
    ]

    return timeline.map((timelineStep, timelineIndex) => {
      const liquidState = timelineStep.robotState.liquidState.labware
      return mapValues(
        liquidState,
        (
          labwareLiquids: StepGeneration.SingleLabwareLiquidState,
          labwareId: string
        ) =>
          _wellContentsForLabware(
            labwareLiquids,
            labwareEntities[labwareId].def
          )
      )
    })
  }
)

export const getLastValidWellContents: Selector<WellContentsByLabware> = createSelector(
  fileDataSelectors.lastValidRobotState,
  stepFormSelectors.getLabwareEntities,
  (robotState, labwareEntities) => {
    return mapValues(
      robotState.labware,
      (
        labwareLiquids: StepGeneration.SingleLabwareLiquidState,
        labwareId: string
      ) => {
        return _wellContentsForLabware(
          robotState.liquidState.labware[labwareId],
          labwareEntities[labwareId].def
        )
      }
    )
  }
)

export const getAllWellContentsForActiveItem: Selector<WellContentsByLabware> = createSelector(
  stepsSelectors.getActiveItem,
  fileDataSelectors.getInitialRobotState,
  fileDataSelectors.getRobotStateTimeline,
  fileDataSelectors.lastValidRobotState,
  stepFormSelectors.getLabwareEntities,
  stepFormSelectors.getOrderedStepIds,
  (
    activeItem,
    initialRobotState,
    robotStateTimeline,
    lastValidRobotState,
    labwareEntities,
    orderedStepIds
  ) => {
    const timeline = [
      { robotState: initialRobotState },
      ...robotStateTimeline.timeline,
      { robotState: lastValidRobotState },
    ]
    const lastValidRobotStateIdx = timeline.length - 1
    let timelineIdx = lastValidRobotStateIdx // default to last valid robot state

    if (activeItem.isStep) {
      timelineIdx = Math.min(
        orderedStepIds.findIndex(id => id === activeItem.id),
        lastValidRobotStateIdx
      )
    } else if (activeItem.id === START_TERMINAL_ITEM_ID) {
      timelineIdx = 0
    }

    assert(
      timelineIdx !== -1,
      `getAllWellContentsForActiveItem got unhandled terminal id: "${
        activeItem.id
      }"`
    )

    const liquidState = timeline[timelineIdx].robotState.liquidState.labware
    const wellContentsByLabwareId = mapValues(
      liquidState,
      (
        labwareLiquids: StepGeneration.SingleLabwareLiquidState,
        labwareId: string
      ) =>
        _wellContentsForLabware(labwareLiquids, labwareEntities[labwareId].def)
    )
    return wellContentsByLabwareId
  }
)

export const getSelectedWellsMaxVolume: Selector<number> = createSelector(
  wellSelectionSelectors.getSelectedWells,
  labwareIngredSelectors.getSelectedLabwareId,
  stepFormSelectors.getLabwareEntities,
  (selectedWells, selectedLabwareId, labwareEntities) => {
    const def = selectedLabwareId && labwareEntities[selectedLabwareId].def
    if (!def) {
      console.warn('No container type selected, cannot get max volume')
      return Infinity
    }
    const maxVolumesByWell = getMaxVolumes(def)
    const maxVolumesList = !isEmpty(selectedWells)
      ? // when wells are selected, only look at vols of selected wells
        Object.values(pick(maxVolumesByWell, Object.keys(selectedWells)))
      : // when no wells selected (eg editing ingred group), look at all volumes.
        // TODO LATER: look at filled wells, not all wells.
        Object.values(maxVolumesByWell)
    return min(maxVolumesList.map(n => parseInt(n)))
  }
)

type CommonWellValues = { ingredientId: ?string, volume: ?number }
/** Returns the common single ingredient group of selected wells,
 * or null if there is not a single common ingredient group */
export const getSelectedWellsCommonValues: Selector<CommonWellValues> = createSelector(
  wellSelectionSelectors.getSelectedWells,
  labwareIngredSelectors.getSelectedLabwareId,
  labwareIngredSelectors.getLiquidsByLabwareId,
  (selectedWells, labwareId, allIngreds) => {
    if (!labwareId) return { ingredientId: null, volume: null }
    const ingredsInLabware = allIngreds[labwareId]
    if (!ingredsInLabware || isEmpty(selectedWells))
      return { ingredientId: null, volume: null }

    const initialWellContents: ?StepGeneration.LocationLiquidState =
      ingredsInLabware[Object.keys(selectedWells)[0]] // TODO IMMEDIATELY why arbitrary 0th???
    const initialIngredId: ?string =
      initialWellContents && Object.keys(initialWellContents)[0]

    const hasCommonIngred = Object.keys(selectedWells).every((well: string) => {
      if (!ingredsInLabware[well]) return null
      const ingreds = Object.keys(ingredsInLabware[well])
      return ingreds.length === 1 && ingreds[0] === initialIngredId
    })

    if (!hasCommonIngred || !initialIngredId || !initialWellContents) {
      return { ingredientId: null, volume: null }
    } else {
      const initialVolume: ?number = initialWellContents[initialIngredId].volume
      const hasCommonVolume = Object.keys(selectedWells).every(
        (well: string) => {
          if (!ingredsInLabware[well] || !initialIngredId) return null
          return (
            ingredsInLabware[well][initialIngredId].volume === initialVolume
          )
        }
      )
      return {
        ingredientId: initialIngredId,
        volume: hasCommonVolume ? initialVolume : null,
      }
    }
  }
)

export const getSelectedWellsCommonIngredId: Selector<?string> = createSelector(
  getSelectedWellsCommonValues,
  commonValues => commonValues.ingredientId || null
)

export const getSelectedWellsCommonVolume: Selector<?number> = createSelector(
  getSelectedWellsCommonValues,
  commonValues => commonValues.volume || null
)
